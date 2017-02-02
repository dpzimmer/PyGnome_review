'''
oil removal from various cleanup options
add these as weatherers
'''
from __future__ import division

import pytest
import datetime
import copy
import unit_conversion as uc
import json
import os
import logging

from colander import (drop, SchemaNode, MappingSchema, Integer, Float, String, OneOf)

from gnome.weatherers import Weatherer
from gnome.utilities.serializable import Serializable, Field
from gnome.persist.extend_colander import LocalDateTime, DefaultTupleSchema, NumpyArray
from gnome.persist import validators, base_schema

from gnome.weatherers.core import WeathererSchema
from gnome import _valid_units


# define valid units at module scope because the Schema and Object both use it
_valid_dist_units = _valid_units('Length')
_valid_vel_units = _valid_units('Velocity')
_valid_vol_units = _valid_units('Volume')
_valid_dis_units = _valid_units('Discharge')
_valid_time_units = _valid_units('Time')
_valid_concentration_units = _valid_units('Oil Concentration')


class OnSceneTupleSchema(DefaultTupleSchema):
    start = SchemaNode(LocalDateTime(default_tzinfo=None),
                       validator=validators.convertible_to_seconds)

    stop = SchemaNode(LocalDateTime(default_tzinfo=None),
                      validator=validators.convertible_to_seconds)


class OnSceneTimeSeriesSchema(NumpyArray):
    value = OnSceneTupleSchema()

    def validator(self, node, cstruct):
        '''
        validate on-scene timeseries list
        '''
        validators.no_duplicate_datetime(node, cstruct)
        validators.ascending_datetime(node, cstruct)


class ResponseSchema(WeathererSchema):
    timeseries = OnSceneTimeSeriesSchema()


class Response(Weatherer, Serializable):

    def __init__(self, **kwargs):
        super(Response, self).__init__(**kwargs)
        self._report = []

    def _get_thickness(self, sc):
        oil_thickness = 0.0
        substance = self._get_substance(sc)
        if sc['area'].any() > 0:
#             pytest.set_trace()
            volume_emul = (sc['mass'].mean() / substance.density_at_temp()) / (1.0 - sc['frac_water'].mean())
            oil_thickness = volume_emul / sc['area'].mean()

        return uc.convert('Length', 'meters', 'inches', oil_thickness)

    @property
    def units(self):
        return self._units

    @units.setter
    def units(self, u_dict):
        for prop, unit in u_dict.iteritems():
            if prop in self._units_type:
                if unit not in self._units_type[prop][1]:
                    msg = ("{0} are invalid units for {1}."
                           "Ignore it".format(unit, prop))
                    self.logger.error(msg)
                    raise uc.InvalidUnitError(msg)

            self._units[prop] = unit

    def get(self, attr, unit=None):
        val = getattr(self, attr)
        if unit is None:
            if (attr not in self._si_units or
                    self._is_units[attr] == self.units[attr]):
                return val
            else:
                unit = self._si_units[attr]

        if unit in self._units_type[attr][1]:
            return uc.convert(self._units_type[attr][0], self.units[attr],
                             unit, val)
        else:
            ex = uc.InvalidUnitError((unit, self._units_type[attr][0]))
            self.logger.error(str(ex))
            raise ex

    def set(self, attr, value, unit):
        if unit not in self._units_type[attr][0]:
            raise uc.InvalidUnitError((unit, self._units_type[attr][0]))

        setattr(self, attr, value)
        self.units[attr] = unit

    def _is_active(self, model_time, time_step):
        for t in self.timeseries:
            if model_time >= t[0] and model_time + datetime.timedelta(seconds=time_step / 2) <= t[1]:
                return True

        return False

    def _setup_report(self, sc):
        if 'report' not in sc:
            sc.report = {}

        sc.report[self.id] = []
        self.report = sc.report[self.id]

    def _get_substance(self, sc):
        '''
        return a single substance - recovery options only know about the
        total amount removed. Unclear how to assign this to multiple substances
        for now, just log an error if more than one substance is present
        '''
        substance = sc.get_substances(complete=False)
        if len(substance) > 1:
            self.logger.error('Found more than one type of oil '
                              '- not supported. Results with be incorrect')

        return substance[0]

    def _remove_mass_simple(self, data, amount):
        total_mass = data['mass'].sum()
        rm_mass_frac = min(amount / total_mass, 1.0)
        data['mass_components'] = \
            (1 - rm_mass_frac) * data['mass_components']
        data['mass'] = data['mass_components'].sum(1)


class Platform(Serializable):
    base_dir = os.path.dirname(__file__)
    with open(os.path.join(base_dir, 'platforms.json'), 'r') as f:
        plat_types = json.load(f)
        plat_types['vessel'] = dict(zip([t['name'] for t in plat_types['vessel']], plat_types['vessel']))
        plat_types['aircraft'] = dict(zip([t['name'] for t in plat_types['aircraft']], plat_types['aircraft']))

    def __init__(self,
                 type_,
                 kind=None,
                 **kwargs):

        if kwargs is None:
            if kind is None:
                n = 'Typical Large Vessel' if type_ == 'vessel' else 'Test Platform'
                for k, attr in Platform.plat_types[type_][n]:
                    setattr(self, k, attr)
        else:
            if kind is not None:
                for k, attr in Platform.plat_types[type_][kind]:
                    setattr(self, k, attr)
            for k, attr in kwargs:
                setattr(self, k, attr)
        self.type = type_

        if self.type == 'aircraft':
            self._all_states = {'cascade': 'refuel',
                                'refuel': 'reload',
                                'reload': 'taxi_takeoff',
                                'taxi_takeoff': 'en_route',
                                'en_route': 'at_site',
                                'at_site': 'disperse',
                                'disperse': 'return',
                                'return': 'landing_taxi',
                                'landing_taxi': 'refuel'}
        else:
            self._all_states = {'cascade': 'refuel',
                                'refuel': 'reload',
                                'reload': 'en_route',
                                'en_route': 'at_site',
                                'at_site': 'disperse',
                                'disperse': 'return',
                                'return': 'refuel'}

        self._state = None
        self._time_to_next = None
        self.active = False
        self.has_payload = False
        self._state = 'cascade'
        self._disp_remaining = 0

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, s):
        self._state = s

    def get_next_state(self):
        return self._all_states[self.state]

    def goto_next_state(self):
        self.state = self._all_states[self.state]

    def get_state_time(self, dist=None, payload=False):
        d = {'cascade': self.cascade_time(dist, payload),
             'refuel': self.refuel,
             'reload': self.reload,
             'taxi_takeoff': self.taxi_time_takeoff,
             'en_route': self.transit_time(dist, payload=True),
             'at_site': self.max_onsite_time(dist),
             'disperse': None,
             'return': self.transit_time(dist, payload=False),
             'landing_taxi': self.taxi_time_landing}

    def release_rate(self, dosage):
        return (dosage * self.application_speed * self.swadth_width) / 430

    def one_way_transit_time(self, dist):
        raw = dist / self.transit_speed
        if self.type == 'aircraft':
            raw += self.taxi_land_depart / 60
        return raw * 60

    def max_dosage(self):
        return (self.pump_rate_max * 430) / (self.application_speed_min * self.swath_width_min)

    def min_dosage(self):
        return (self.pump_rate_min * 430) / (self.application_speed_max * self.swath_width_max)

    def cascade_time(self, dist, payload=False):
        max_range = self.max_range_with_payload if payload else self.max_range_no_payload
        speed = self.cascade_transit_speed_with_payload if payload else self.cascade_transit_speed_without_payload

        cascade_time = 0
        if dist > max_range:
            num_legs = dist / max_range
            frac_leg = (num_legs * 1000) % 1000
            num_legs = int(num_legs)
            cascade_time += self.taxi_land_depart / 60
            cascade_time += (num_legs * max_range)
            inter_stop = (self.taxi_land_depart * 2 + self.fuel_load) / 60
            cascade_time += num_legs * inter_stop
            cascade_time += frac_leg * (max_range / speed)
            cascade_time += self.taxi_land_depart / 60
        else:
            cascade_time += self.taxi_land_depart / 60 * 2
            cascade_time += dist / speed
        return cascade_time

    def transit_time(self, dist, payload=False):
        return dist / self.transit_speed

    def max_onsite_time(self, dist):
        rv = self.max_op_time - 2 * dist / self.transit_speed
        if rv < 0:
            if rv > (self.payload / self.max_pump_rate):
                logging.warn("max onsite time is less than time expected to spray")
            else:
                logging.warn('max onsite time is less than zero')
        return rv

    def sortie_time(self, dist, simul_reload=False):
        rel = max(self.fuel_load, self.dispersant_load) if simul_reload else self.fuel_load + self.dispersant_load
        sortie = rel + self.taxi_land_depart + self.transit_time(dist, payload=True) + self.taxi_land_arrival
        return sortie


class PlatformUnitsSchema(MappingSchema):
    swath_width = approach_distance = depart_distance = SchemaNode(String(),
                                                                   description='SI units for distance',
                                                                   validator=OneOf(_valid_dist_units))
    application_speed = transit_speed = reposition_speed = SchemaNode(String(),
                                                                      description='SI units for speed',
                                                                      validator=OneOf(_valid_vel_units))
    pump_rate = SchemaNode(String(),
                           description='SI units for discharge',
                           validator=OneOf(_valid_dis_units))
    payload = SchemaNode(String(),
                         description='SI units for volume',
                         validator=OneOf(_valid_vol_units))
    max_operating_time = u_turn_time = takeoff_land_time = reload = refuel = SchemaNode(String(),
                                                                                        description='SI units for time',
                                                                                        validator=OneOf(_valid_time_units))


class DisperseUnitsSchema(MappingSchema):
    transit = pass_length = cascade_distance = SchemaNode(String(),
                                                          description='SI units for distance',
                                                          validator=OneOf(_valid_dist_units))
    dosage_unit = SchemaNode(String(),
                             description='SI units for concentration',
                             validator=OneOf(_valid_concentration_units))

class DisperseSchema(ResponseSchema):
    pass

class Disperse(Response):

    _si_units = {'transit': 'nm',
                 'pass_length': 'nm',
                 'cascade_distance': 'nm',
                 'dosage': 'gal/acre'}

    plat_units = dict([(k, 'minutes') for k in ['taxi_time_landing',
                                                'taxi_time_takeoff',
                                                'takeoff_land_time'
                                                'reload',
                                                'refuel',
                                                'u_turn_time',
                                                'staging_area_brief']])
    plat_units.update([(k, 'hours') for k in ['max_op_time']])
    plat_units.update([(k, 'gal') for k in ['payload']])

    _units_types = dict([(i[0], uc.UNIT_TYPES.get(i[1], None)) for i in (plat_units.items() + _si_units.items())])
    _units_types['dosage'] = 'oilconcentration'  #because oil concentration is a pita

    base_dir = os.path.dirname(__file__)
    with open(os.path.join(base_dir, 'platforms.json'), 'r') as f:
        js = json.load(f)
        plat_types = dict(zip([t['name'] for t in js['vessel']], js['vessel']))
        plat_types.update(dict(zip([t['name'] for t in js['aircraft']], js['aircraft'])))

    _state = copy.deepcopy(Response._state)
    _state += [Field('offset', save=True, update=True)]

    def __init__(self,
                 name=None,
                 transit=None,
                 pass_length=4,
                 dosage=None,
                 cascade_on=False,
                 cascade_distance=None,
                 timeseries=None,
                 loading_type='simultaneous',
                 pass_type='bidirectional',
                 platform=None,
                 **kwargs):
        super(Disperse, self).__init__(**kwargs)
        self.name = name
        self.transit = transit
        self.pass_length = pass_length
        self.dosage = dosage
        self.cascade_on = cascade_on
        self.cascade_distance = cascade_distance
        self.loading_type = loading_type
        self.pass_type = pass_type
        self.cur_state = self.prev_state = None
        # time to next state
        self._ttns = 0
        if platform is not None:
            if isinstance(platform, basestring):
                #find platform name
                self.platform = Disperse.plat_types[platform]
            else:
                #platform is defined as a dict
                self.platform = platform
#         pytest.set_trace()


    @property
    def next_state(self):
        if self.cur_state is None:
            return None
        if self.cur_state == 'cascade' or self.cur_state == 'rtb':
            return 'replenish'
        elif self.cur_state == 'replenish':
            return 'en_route'
        elif self.cur_state == 'en_route':
            return 'on_site'

    def conv_platform(self):
        pass

    def uconv(self, attr, ):
        if attr not in Disperse._units_types:
            raise TypeError('invalid attribute or attribute does not have a unit')
        u1 = Disperse._units_types[attr]
        u2 = None
        if u1 == 'time':
            u2 = 'sec'
        elif u1 == 'length':
            u2 = 'nm'
        elif u1 == 'volume':
            u2 = 'gal'
        elif u1 == 'speed':
            u2 = 'knots'
        else:
            u2 = u1
        v1 = None
        try:
            u1 = Disperse.plat_units[attr]
            v1 = self.platform[attr]
        except KeyError:
            try:
                u1 = Disperse._si_units[attr]
                v1 = getattr(self, attr)
            except KeyError:
                'Invalid Attr'
        print self.platform
        v2 = uc.convert(u1, u2, v1)
        return v2

    @property
    def cur_stage_duration(self):
        if self.cur_state is None:
            return None
        if self.cur_state == 'cascade':
            return Disperse.uconv(self.platform.cascade_time(self.cascade, payload=False))
        if self.cur_state == 'replenish':
            if self.loading_type =='simultaneous':
                return Disperse.uconv(max(self.platform.reload, self.platform.refuel))
            else:
                return Disperse.uconv(self.platform.reload + self.platform.refuel)
        if self.cur_state == 'en_route':
            return

#     @property
#     def platform(self):
#         return self._platform.to_dict()
#
#     @platform.setter
#     def platform(self, plt):
#         _type = None
#         if not isinstance(plt, basestring):
#             if 'type' in plt.keys():
#                 _type = plt.pop('type')
#             elif plt['name'] in Platform.plat_types['aircraft'].keys():
#                 _type = 'aircraft'
#             elif plt['name'] in Platform.plat_types['vessel'].keys():
#                 _type = 'vessel'
#             else:
#                 raise ValueError('Must specify platform type (aircraft/vessel)')
#             self._platform = Platform(_type,
#                                       kind=plt.pop('name', None),
#                                       **plt)
#         else:
#             p = Platform.plat_types['aircraft'].get(plt, None)
#             if p is None:
#                 p = Platform.plat_types['vessel'].get(plt, None)
#             if p is None:
#                 raise ValueError("Specified platform was not found")
#             self.platform = p

#     def prepare_for_model_run(self, sc):
#         self._setup_report(sc)
#         self._swath_width = 0.3 * self.boom_length
#         self._area = self._swath_width * (0.4125 * self.boom_length / 3) * 2 / 3
#         self._boom_capacity = self.boom_draft / 36 * self._area
#         self._boom_capacity_remaining = self._boom_capacity
#         self._offset_time = (self.offset * 0.00987 / self.speed) * 60
#         self._area_coverage_rate = self._swath_width * self.speed / 430
#
#         if self._swath_width > 1000:
#             self.report.append('Swaths > 1000 feet may not be achievable in the field')
#
#         if self.speed > 1.2:
#             self.report.append('Excessive entrainment of oil likely to occur at speeds greater than 1.2 knots.')
#
#         if self.on:
#             sc.mass_balance['burned'] = 0.0
#             sc.mass_balance[self.id] = 0.0
#             sc.mass_balance['boomed'] = 0.0
#
#         self._is_collecting = True
#
#     def prepare_for_model_step(self, sc, time_step, model_time):
#         '''
#         1. set 'active' flag based on timeseries and model_time
#         2. Mark LEs to be burned, do them in order right now. assume all LEs
#            that are released together will be burned together since they would
#            be closer to each other in position.
#         '''
#
#         self._ts_collected = 0.
#         self._ts_burned = 0.
#
#         if self._is_active(model_time, time_step):
#             self._active = True
#         else:
#             self._active = False
#
#         if not self.active:
#             return
#
#         self._time_remaining = time_step
#
#         while self._time_remaining > 0.:
#             if self._is_collecting:
#                 self._collect(sc, time_step, model_time)
#
#             if self._is_transiting and self._is_boom_full:
#                 self._transit(sc, time_step, model_time)
#
#             if self._is_burning:
#                 self._burn(sc, time_step, model_time)
#
#             if self._is_cleaning:
#                 self._clean(sc, time_step, model_time)
#
#             if self._is_transiting and not self._is_boom_full:
#                 self._transit(sc, time_step, model_time)

    def prepare_for_model_run(self, sc):
        self._setup_report(sc)
        p = self.platform
        p.active = False
        p._disp_remaining = 0
        p._time_remaining = 0
        if self.cascade_on:
            p._stage = 'cascade'
            p._ttns = self.platform.cascade_time(self.cascade_distance, payload=False)
        else:
            p._stage = 'en_route'
            p._disp_remaining = self.payload
            p._ttns = p.transit_time(self.transit, payload=True)

    def prepare_for_model_step(self, sc, time_step, model_time):
        '''
        '''
        extra_time = 0
        p = self.platform
        p._ttns -= time_step
        if p._ttns < 0:
            extra_time = abs(p._ttns)
            p.goto_next_state()
            st = p.get_state_time(self.transit, payload=p.has_payload)
            if st is None:
                # DISPERSION STATE. Implementation for the time remaining in this stage is below
                desired_dosage = self.dosage
                achieved_pump_rate = desired_dosage * p.application_speed * p.swath_width


            p._ttns = 5
        elif p._ttns == 0:
            p.goto_next_state()
            p._ttns = 5


class BurnUnitsSchema(MappingSchema):
    offset = SchemaNode(String(),
                        description='SI units for distance',
                        validator=OneOf(_valid_dist_units))

    boom_length = SchemaNode(String(),
                             description='SI units for distance',
                             validator=OneOf(_valid_dist_units))

    boom_draft = SchemaNode(String(),
                            description='SI units for distance',
                            validator=OneOf(_valid_dist_units))

    speed = SchemaNode(String(),
                       description='SI units for speed',
                       validator=OneOf(_valid_vel_units))

class BurnSchema(ResponseSchema):
    offset = SchemaNode(Integer())
    boom_length = SchemaNode(Integer())
    boom_draft = SchemaNode(Integer())
    speed = SchemaNode(Float())
    throughput = SchemaNode(Float())
    burn_efficiency_type = SchemaNode(String())
    units = BurnUnitsSchema()

class Burn(Response):
    _state = copy.deepcopy(Response._state)
    _state += [Field('offset', save=True, update=True),
               Field('boom_length', save=True, update=True),
               Field('boom_draft', save=True, update=True),
               Field('speed', save=True, update=True),
               Field('timeseries', save=True, update=True),
               Field('throughput', save=True, update=True),
               Field('burn_efficiency_type', save=True, update=True),
               Field('units', save=True, update=True)]

    _schema = BurnSchema

    _si_units = {'offset': 'ft',
                 'boom_length': 'ft',
                 'boom_draft': 'in',
                 'speed': 'kts'}

    _units_type = {'offset': ('offset', _valid_dist_units),
                   'boom_length': ('boom_length', _valid_dist_units),
                   'boom_draft': ('boom_draft', _valid_dist_units),
                   'speed': ('speed', _valid_vel_units)}

    def __init__(self,
                 offset,
                 boom_length,
                 boom_draft,
                 speed,
                 throughput,
                 burn_efficiency_type=1,
                 timeseries=None,
                 units=_si_units,
                 **kwargs):

        super(Burn, self).__init__(**kwargs)

        self.offset = offset
        self._units = dict(self._si_units)
        self.units = units
        self.boom_length = boom_length
        self.boom_draft = boom_draft
        self.speed = speed
        self.throughput = throughput
        self.timeseries = timeseries
        self.burn_efficiency_type = burn_efficiency_type
        self._swath_width = None
        self._area = None
        self._boom_capacity = None
        self._offset_time = None

        self._is_collecting = False
        self._is_burning = False
        self._is_boom_filled = False
        self._is_transiting = False
        self._is_cleaning = False

        self._time_collecting_in_sim = 0.
        self._total_burns = 0.
        self._time_burning = 0.
        self._ts_burned = 0.
        self._ts_collected = 0.
        self._burn_time = None

    def prepare_for_model_run(self, sc):
        self._setup_report(sc)
        self._swath_width = 0.3 * self.boom_length
        self._area = self._swath_width * (0.4125 * self.boom_length / 3) * 2 / 3
        self._boom_capacity = self.boom_draft / 36 * self._area
        self._boom_capacity_remaining = self._boom_capacity
        self._offset_time = (self.offset * 0.00987 / self.speed) * 60
        self._area_coverage_rate = self._swath_width * self.speed / 430

        if self._swath_width > 1000:
            self.report.append('Swaths > 1000 feet may not be achievable in the field')

        if self.speed > 1.2:
            self.report.append('Excessive entrainment of oil likely to occur at speeds greater than 1.2 knots.')

        if self.on:
            sc.mass_balance['burned'] = 0.0
            sc.mass_balance[self.id] = 0.0
            sc.mass_balance['boomed'] = 0.0

        self._is_collecting = True

    def prepare_for_model_step(self, sc, time_step, model_time):
        '''
        1. set 'active' flag based on timeseries and model_time
        2. Mark LEs to be burned, do them in order right now. assume all LEs
           that are released together will be burned together since they would
           be closer to each other in position.
        '''

        self._ts_collected = 0.
        self._ts_burned = 0.

        if self._is_active(model_time, time_step):
            self._active = True
        else:
            self._active = False

        if not self.active:
            return

        self._time_remaining = time_step

        while self._time_remaining > 0.:
            if self._is_collecting:
                self._collect(sc, time_step, model_time)

            if self._is_transiting and self._is_boom_full:
                self._transit(sc, time_step, model_time)

            if self._is_burning:
                self._burn(sc, time_step, model_time)

            if self._is_cleaning:
                self._clean(sc, time_step, model_time)

            if self._is_transiting and not self._is_boom_full:
                self._transit(sc, time_step, model_time)

    def _collect(self, sc, time_step, model_time):
        # calculate amount collected this time_step
        if self._burn_time is None:
            self._burn_rate = 0.14 * (100 - (sc['frac_water'].mean() * 100)) / 100
            self._burn_time = (0.33 * self.boom_draft / self._burn_rate) * 60
            self._burn_time_remaining = self._burn_time

        oil_thickness = self._get_thickness(sc)
        encounter_rate = 63.13 * self._swath_width * oil_thickness * self.speed
        emulsion_rr = encounter_rate * self.throughput
        if oil_thickness > 0:
            # old ROC equation
            # time_to_fill = (self._boom_capacity_remaining / emulsion_rr) * 60
            # new ebsp equation
            time_to_fill = ((self._boom_capacity_remaining * 0.17811) * 42) / emulsion_rr
        else:
            time_to_fill = 0.

        if time_to_fill > self._time_remaining:
            # doesn't finish fill the boom in this time step
            self._ts_collected = emulsion_rr * (self._time_remaining / 60)
            self._boom_capacity_remaining -= self.collected
            self._time_remaining = 0.0
            self._time_collecting_in_sim += self._time_remaining
        elif self._time_remaining > 0:
            # finishes filling the boom in this time step any time remaining
            # should be spend transiting to the burn position
            self._ts_collected = self._boom_capacity_remaining

            self._boom_capacity_remaining = 0.0
            self._is_boom_full = True

            self._time_remaining -= time_to_fill
            self._time_collecting_in_sim += time_to_fill
            self._offset_time_remaining = self._offset_time
            self._is_collecting = False
            self._is_transiting = True

    def _transit(self, sc, time_step, model_time):
        # transiting to burn site
        # does it arrive and start burning?
        if self._time_remaining > self._offset_time_remaining:
            self._time_remaining -= self._offset_time_remaining
            self._offset_time_remaining = 0.
            self._is_transiting = False
            if self._is_boom_full:
                self._is_burning = True
            else:
                self._is_collecting = True
        elif self._time_remaining > 0:
            self._offset_time_remaining -= self._time_remaining
            self._time_remaining = 0.

    def _burn(self, sc, time_step, model_time):
        # burning
        self._is_boom_full = False
        if self._time_remaining > self._burn_time_remaining:
            self._time_remaining -= self._burn_time_remaining
            self._burn_time_remaining = 0.
            burned = self._boom_capacity - self._boom_capacity_remaining
            self._ts_burned = burned
            self._is_burning = False
            self._is_cleaning = True
            self._cleaning_time_remaining = 3600  # 1hr in seconds
        elif self._time_remaining > 0:
            frac_burned = self._time_remaining / self._burn_time
            burned = self._boom_capacity * frac_burned
            self._boom_capacity_remaining += burned
            self._ts_burned = burned
            self._burn_time_remaining -= self._time_remaining
            self._time_remaining = 0.

    def _clean(self, sc, time_step, model_time):
        # cleaning
        self._burn_time = None
        if self._time_remaining > self._cleaning_time_remaining:
            self._time_remaining -= self._cleaning_time_remaining
            self._cleaning_time_remaining = 0.
            self._is_cleaning = False
            self._is_transiting = True
            self._offset_time_remaining = self._offset_time
        elif self._time_remaining > 0:
            self._cleaning_time_remaining -= self._time_remaining
            self._time_remaining = 0.

    def weather_elements(self, sc, time_step, model_time):
        '''
        Remove mass from each le equally for now, no flagging for not
        just make sure it's from floating oil.
        '''
        if not self.active or len(sc) == 0:
            return

        les = sc.itersubstancedata(self.array_types)
        for substance, data in les:
            if len(data['mass']) is 0:
                continue

            if self._ts_collected:
                sc.mass_balance['boomed'] += self._ts_collected
                sc.mass_balance[self.id] += self._ts_collected
                self._remove_mass_simple(data, self._ts_collected)

                self.logger.debug('{0} amount boomed for {1}: {2}'
                                  .format(self._pid, substance.name, self._ts_collected))

            if self._ts_burned:
                sc.mass_balance['burned'] += self._ts_burned
                sc.mass_balance['boomed'] -= self._ts_burned

class SkimUnitsSchema(MappingSchema):
    storage = SchemaNode(String(),
                         description='SI units for onboard storage',
                         validator=OneOf(_valid_vol_units))

    decant_pump = SchemaNode(String(),
                             description='SI units for discharge',
                             validator=OneOf(_valid_dis_units))

    nameplate_pump = SchemaNode(String(),
                             description='SI units for discharge',
                             validator=OneOf(_valid_dis_units))

    speed = SchemaNode(String(),
                       description='SI units for speed',
                       validator=OneOf(_valid_vel_units))

    swath_width = SchemaNode(String(),
                             description='SI units for length',
                             validator=OneOf(_valid_dist_units))

class SkimSchema(ResponseSchema):
    units = SkimUnitsSchema()
    speed = SchemaNode(Float())
    storage = SchemaNode(Float())
    swath_width = SchemaNode(Float())
    group = SchemaNode(String())
    throughput = SchemaNode(Float())
    nameplate_pump = SchemaNode(Float())
    skim_efficiency_type = SchemaNode(String())
    decant = SchemaNode(Float())
    decant_pump = SchemaNode(Float())
    rig_time = SchemaNode(Float())
    transit_time = SchemaNode(Float())
    offload_to = SchemaNode(String())
    recovery = SchemaNode(String())
    recovery_ef = SchemaNode(Float())
    barge_arrival = SchemaNode(LocalDateTime(),
                               validator=validators.convertible_to_seconds,
                               missing=drop)


class Skim(Response):
    _state = copy.deepcopy(Response._state)
    _state += [Field('units', save=True, update=True),
               Field('speed', save=True, update=True),
               Field('storage', save=True, update=True),
               Field('swath_width', save=True, update=True),
               Field('group', save=True, update=True),
               Field('throughput', save=True, update=True),
               Field('nameplate_pump', save=True, update=True),
               Field('skim_efficiency_type', save=True, update=True),
               Field('decant', save=True, update=True),
               Field('decant_pump', save=True, update=True),
               Field('rig_time', save=True, update=True),
               Field('transit_time', save=True, update=True),
               Field('offload_to', save=True, update=True),
               Field('barge_arrival', save=True, update=True),
               Field('recovery', save=True, update=True),
               Field('recovery_ef', save=True, update=True)]

    _schema = SkimSchema

    _si_units = {'storage': 'bbl',
                 'decant_pump': 'gpm',
                 'nameplate_pump': 'gpm',
                 'speed': 'kts',
                 'swath_width': 'ft',
                 'transit_time': 'sec',
                 'offload': 'sec',
                 'rig_time': 'sec'}

    _units_types = {'storage': ('storage', _valid_vol_units),
                    'decant_pump': ('decant_pump', _valid_dis_units),
                    'nameplate_pump': ('nameplate_pump', _valid_dis_units),
                    'speed': ('speed', _valid_vel_units),
                    'swath_width': ('swath_width', _valid_dist_units),
                    'transit_time': ('transit_time', _valid_time_units),
                    'offload': ('offload', _valid_time_units),
                    'rig_time': ('rig_time', _valid_time_units)}

    def __init__(self,
                 speed,
                 storage,
                 swath_width,
                 group,
                 throughput,
                 nameplate_pump,
                 recovery,
                 recovery_ef,
                 decant,
                 decant_pump,
                 rig_time,
                 transit_time,
                 offload_to,
                 barge_arrival,
                 units=_si_units,
                 **kwargs):

        super(Skim, self).__init__(**kwargs)

        self.speed = speed
        self.storage = storage
        self.swath_width = swath_width
        self.group = group
        self.throughput = throughput
        self.nameplate_pump = nameplate_pump
        self.recovery = recovery
        self.recovery_ef = recovery_ef
        self.decant = decant
        self.decant_pump = decant_pump
        self.rig_time = rig_time
        self.transit_time = transit_time
        self.offload_to = offload_to
        self.barge_arrival = barge_arrival
        self._units = dict(self._si_units)

        self._is_collecting = False
        self._is_transiting = False
        self._is_offloading = False
        self._is_rig_deriging = False

    def prepare_for_model_run(self, sc):
        self._setup_report(sc)
        self._storage_remaining = self.storage
        self._coverage_rate = self.swath_width * self.speed * 0.00233

        if self.on:
            sc.mass_balance['skimmed'] = 0.0
            sc.mass_balance[self.id] = {'fluid_collected': 0.0,
                                        'emulsion_collected': 0.0,
                                        'oil_collected': 0.0,
                                        'water_collected': 0.0,
                                        'water_decanted': 0.0,
                                        'water_retained': 0.0,
                                        'area_covered': 0.0,
                                        'storage_remaining': 0.0}

        self._is_collecting = True

    def prepare_for_model_step(self, sc, time_step, model_time):
        if self._is_active(model_time, time_step):
            self._active = True
        else :
            self._active = False

        if not self.active:
            return

        self._time_remaining = time_step

        if type(self.barge_arrival) is datetime.date:
            # if there's a barge so a modified cycle
            while self._time_remaining > 0.:
                if self._is_collecting:
                    self._collect(sc, time_step, model_time)
        else:
            while self_time_remaining > 0.:
                if self._is_collecting:
                    self._collect(sc, time_step, model_time)

                if self._is_transiting:
                    self._transit(sc, time_step, model_time)

                if self._is_offloading:
                    self._offload(sc, time_step, model_time)


    def _collect(self, sc, time_step, model_time):
        thickness = self._get_thickness(sc)
        self._maximum_effective_swath = self.nameplate_pump * self.recovery / (63.13 * self.speed * thickness * self.throughput)

        if self.swath > self._maximum_effective_swath:
            swath = self._maximum_effective_swath;

        if swath > 1000:
            self.report.append('Swaths > 1000 feet may not be achievable in the field.')

        encounter_rate = thickness * self.speed * swath * 63.13
        rate_of_coverage = swath * self.speed * 0.00233

        if encounter_rate > 0:
            recovery = self._getRecoveryEfficiency()

            if recovery > 0:
                totalFluidRecoveryRate = encounter_rate * (self.throughput / recovery)

                if totalFluidRecoveryRate > self.nameplate_pump:
                    # total fluid recovery rate is greater than nameplate
                    # pump, recalculate the throughput efficiency and
                    # total fluid recovery rate again with the new throughput
                    throughput = self.nameplate_pump * recovery / encounter_rate
                    totalFluidRecoveryRate = encounter_rate * (throughput / recovery)
                    msg = ('{0.name} - Total Fluid Recovery Rate is greater than Nameplate \
                            Pump Rate, recalculating Throughput Efficiency').format(self)
                    self.logger.warni(msg)

                if throughput > 0:
                    emulsionRecoveryRate = encounter_rate * throughput

                    waterRecoveryRate = (1 - recovery) * totalFluidRecoveryRate
                    waterRetainedRate = waterRecoveryRate * (1 - self.decant)
                    computedDecantRate = (totalFluidRecoveryRate - emulsionRecoveryRate) * self.decant

                    decantRateDifference = 0.
                    if computedDecantRate > self.decant_pump:
                        decantRateDifference = computedDecantRate - self.decant_pump

                    recoveryRate = emulsionRecoveryRate + waterRecoveryRate
                    retainRate = emulsionRecoveryRate + weaterRetainedRate + decantRateDifference
                    oilRecoveryRate = emlusionRecoveryRate * (1 - sc['frac_water'].mean())

                    freeWaterRecoveryRate = recoveryRate - emulsionRecoveryRate
                    freeWaterRetainedRate = retainRate - emulsionRecoveryRate
                    freeWaterDecantRate = freeWaterRecoveryRate - freeWaterRetainedRate

                    timeToFill = .7 * self._storage_remaining / retainRate * 60

                    if timeToFill * 60 > self._time_remaining:
                        # going to take more than this timestep to fill the storage
                        time_collecting = self._time_remaining
                        self._time_remaining = 0.
                    else:
                        # storage is filled during this timestep
                        time_collecting = timeToFill
                        self._time_remaining -= timeToFill
                        self._transit_remaining = self.transit
                        self._collecting = False
                        self._transiting = True

                    self._ts_fluid_collected = retainRate * time_collecting
                    self._ts_emulsion_collected = emulsionRecoveryRate * time_collecting
                    self._ts_oil_collected = oilRecoveryRate * time_collecting
                    self._ts_water_collected = freeWaterRecoveryRate * time_collecting
                    self._ts_water_decanted = freeWaterDecantRate * time_collecting
                    self._ts_water_retained = freeWaterRetainedRate * time_collecting
                    self._ts_area_covered = rate_of_coverage * time_collecting

                    self._storage_remaining -= uc.convert('Volume', 'gal', 'bbl', self._ts_fluid_collected)

    def _transit(self, sc, time_step, model_time):
        # transiting back to shore to offload
        if self._time_remaining > self._transit_remaining:
            self._time_remaining -= self._transit_remaining
            self._transit_remaining = 0.
            self._is_transiting = False
            if self._storage_remaining == 0.0:
                self._is_offloading = True
            else:
                self._is_collecting = True
            self._offload_remaining = self.offload + self.rig_time
        else:
            self._transit_remaining -= self._time_remaining
            self._time_remaining = 0.

    def _offload(self, sc, time_step, model_time):
        if self._time_remaining > self._offload_remaining:
            self._time_remaining -= self_ofload_remaining
            self._offload_remaining = 0.
            self._storage_remaining = self.storage
            self._offloading = False
            self._transiting = True
        else:
            self._offload_remaining -= self._time_remaining
            self._time_remaining = 0.

    def weather_elements(self, sc, time_step, model_time):
        '''
        Remove mass from each le equally for now, no flagging for now
        just make sure the mass is from floating oil.
        '''
        if not self.active or len(sc) == 0:
            return

        les = sc.itersubstancedata(self.array_types)
        for substance, data in les:
            if len(data['mass']) is 0:
                continue

            if self._ts_oil_collected:
                sc.mass_balance['skimmed'] += self._ts_oil_collected
                self._remove_mass_simple(data, amount)

                self.logger.debug('{0} amount boomed for {1}: {2}'
                                  .format(self._pid, substance.name, self._ts_collected))

                platform_balance = sc.mass_balance[self.id]
                platform_balance['fluid_collected'] += self._ts_fluid_collected
                platform_balance['emulsion_collected'] += self._ts_emulsion_collected
                platform_balance['oil_collected'] += self._ts_oil_collected
                platform_balance['water_collected'] += self._ts_water_collected
                platform_balance['water_retained'] += self._ts_water_retained
                platform_balance['water_decanted'] += self._ts_water_decanted
                platform_balance['area_covered'] += self._ts_area_covered
                platform_balance['storage_remaining'] += self._storage_remaining


    def _getRecoveryEfficiency(self):
        # scaffolding method
        # will eventually include logic for calculating
        # recovery efficiency based on wind and oil visc.

        return self.recovery

if __name__ == '__main__':
    print None
    d = Disperse(name = 'test')
