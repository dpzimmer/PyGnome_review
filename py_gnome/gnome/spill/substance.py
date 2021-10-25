"""
The substance is an abstraction for the various types of
"things" one might model with pyGNOME.

The role of a substance is to:

 - Define what data is carried with the elements

 - Provide a way to initialize the elements

 - Optionally, provide tools to support computation during the run

   - for example, GnomeOil can compute changes in density, etc of
     the elements as the model runs.
"""

import numpy as np

from gnome.basic_types import fate
from gnome.array_types import gat

from gnome.persist import (Float, Int, SchemaNode, SequenceSchema,
                           Boolean, ObjTypeSchema, GeneralGnomeObjectSchema,
                           TupleSchema, Range)

from gnome.gnomeobject import GnomeId
from gnome.spill.initializers import (DistributionBaseSchema,
                                      InitWindages,
                                      InitRiseVelFromDropletSizeFromDist,
                                      )


class WindageRangeSchema(TupleSchema):
    min_windage = SchemaNode(Float(), validator=Range(0, 1.0),
                             default=0.01)

    max_windage = SchemaNode(Float(), validator=Range(0, 1.0),
                             default=0.04)


class SubstanceSchema(ObjTypeSchema):
    # initializers = SequenceSchema(
    #     GeneralGnomeObjectSchema(
    #         acceptable_schemas=[DistributionBaseSchema]
    #     ),
    #     save=True, update=True, save_reference=True
    # )
    windage_range = WindageRangeSchema(
        save=True, update=True,
    )
    windage_persist = SchemaNode(
        Int(), default=900, save=True, update=True,
    )
    is_weatherable = SchemaNode(Boolean(), read_only=True)
    standard_density = SchemaNode(Float(), read_only=True)


class NonWeatheringSubstanceSchema(SubstanceSchema):
    pass


class Substance(GnomeId):
    _schema = SubstanceSchema
    _ref_as = 'substance'

    def __init__(self,
                 windage_range=(.01, .04),
                 windage_persist=900,
                 standard_density=1000.0,
                 *args,
                 **kwargs):
        """
        :param standard_density=1000.0: The density of the substance, used to convert
                                        mass to/from volume
        :type standard_density: Floating point decimal value

        """
        super(Substance, self).__init__(*args, **kwargs)
        self._windage_init = InitWindages(windage_range=windage_range,
                                          windage_persist=windage_persist)
        self.initializers = [self._windage_init]
        # fixme: shouldn't the array_types be defined on this class?
        self.array_types.update(self._windage_init.array_types)
        # fixme: why is this here? why not just set them?
        if windage_range != (.01, .04):
            self.windage_range = windage_range
        if windage_persist != 900:
            self.windage_persist = windage_persist
        try:
            self.standard_density = standard_density
        except AttributeError:
            # this has been overridden in a subclass
            pass

        self.array_types.update({
            'density': gat('density'),
            'fate_status': gat('fate_status')})

    @property
    def all_array_types(self):
        '''
        Fixme: should the initializers be what holds the array types?
                don't we know that this should have already?
        '''
        arr = self.array_types.copy()
        for init in self.initializers:
            arr.update(init.all_array_types)
        return arr

    # fixme: can't we make this a regular attribute??
    @property
    def is_weatherable(self):
        if not hasattr(self, '_is_weatherable'):
            self._is_weatherable = True
        return self._is_weatherable

    @is_weatherable.setter
    def is_weatherable(self, val):
        self._is_weatherable = True if val else False
    '''
    Windage range/persist are important enough to receive properties on the
    Substance.
    '''
    @property
    def windage_range(self):
        if self._windage_init:
            return self._windage_init.windage_range
        else:
            raise ValueError('No windage initializer on this substance')

    @windage_range.setter
    def windage_range(self, val):
        if self._windage_init:
            if np.any(np.asarray(val) < 0) or np.asarray(val).size != 2:
                raise ValueError("'windage_range' >= (0, 0). "
                                 "Nominal values vary between 1% to 4%. "
                                 "Default windage_range=(0.01, 0.04)")
            self._windage_init.windage_range = val
        else:
            raise ValueError('No windage initializer on this substance')

    @property
    def windage_persist(self):
        if self._windage_init:
            return self._windage_init.windage_persist
        else:
            raise ValueError('No windage initializer on this substance')

    @windage_persist.setter
    def windage_persist(self, val):
        if self._windage_init:
            if val == 0:
                raise ValueError("'windage_persist' cannot be 0. "
                                 "For infinite windage, windage_persist=-1 "
                                 "otherwise windage_persist > 0.")
            self._windage_init.windage_persist = val
        else:
            raise ValueError('No windage initializer on this substance')

    def get_initializer_by_name(self, name):
        ''' get first initializer in list whose name matches 'name' '''
        init = [i for i in enumerate(self.initializers) if i.name == name]

        if len(init) == 0:
            return None
        else:
            return init[0]

    def has_initializer(self, name):
        '''
        Returns True if an initializer is present in the list which sets the
        data_array corresponding with 'name', otherwise returns False
        '''
        for i in self.initializers:
            if name in i.array_types:
                return True

        return False

    def initialize_LEs(self, to_rel, arrs):
        '''
        :param to_rel - number of new LEs to initialize
        :param arrs - dict-like of data arrays representing LEs
        '''
        for init in self.initializers:
            init.initialize(to_rel, arrs, self)

    def _attach_default_refs(self, ref_dict):
        for i in self.initializers:
            i._attach_default_refs(ref_dict)
        return GnomeId._attach_default_refs(self, ref_dict)


class NonWeatheringSubstance(Substance):
    _schema = NonWeatheringSubstanceSchema

    """
    The simplest substance that can be used with the model

    It can not be weathered, but does have basic properties for transport:

    Windage, density, etc.
    """

    # def __init__(self,
    #              **kwargs):
    #     """
    #     Initialize a non-weathering substance.

    #     All parameters are optional

    #     :param standard_density=1000.0: The density of the substance, assumed
    #                                     to be measured at 15 C.
    #     :type standard_density: Floating point decimal value

    #     """
    #     # :param pour_point=273.15: The pour_point of the substance, assumed
    #     #                           to be measured in degrees Kelvin.
    #     # :type pour_point: Floating point decimal value

    #     super(NonWeatheringSubstance, self).__init__(**kwargs)

    @property
    def is_weatherable(self):
        if not hasattr(self, '_is_weatherable'):
            self._is_weatherable = False
        return self._is_weatherable

    @is_weatherable.setter
    def is_weatherable(self, val):
        self.logger.warning('This substance {0} cannot be set to be weathering')

    def initialize_LEs(self, to_rel, arrs):
        '''
        :param to_rel - number of new LEs to initialize
        :param arrs - dict-like of data arrays representing LEs
        '''
        sl = slice(-to_rel, None, 1)
        arrs['density'][sl] = self.standard_density
        if ('fate_status' in arrs):
            arrs['fate_status'][sl] = fate.non_weather
        super(NonWeatheringSubstance, self).initialize_LEs(to_rel, arrs)

    def density_at_temp(self, temp=273.15):
        # should this exist ??
        '''
            For non-weathering substance, we just return the standard density.
        '''
        return self.standard_density


class SubsurfaceSubstance(NonWeatheringSubstance):
    """
    Substance that can be used subsurface

    key feature is that it initializes rise velocity from a distribution

    Note: this should probably be part of a Release Object, not a Substance.
    """
    def __init__(self,
                 distribution,
                 *args,
                 **kwargs
                 ):
        """
        :param distribution='UniformDistribution': which distribution to use
        :type distribution: Distribution Object

        Note: distribution should return values in m/s

        See gnome.utilities.distributions for details
        """
        super().__init__(*args, **kwargs)

        init = InitRiseVelFromDropletSizeFromDist(distribution=distribution)
        self.initializers.append(init)
        self.array_types.update(init.array_types)



# so old save files will work
# this should be removed eventually ...
from .gnome_oil import GnomeOil


