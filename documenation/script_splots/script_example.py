#!/usr/bin/env python
"""
Example script to set release from splots, includes diffusion and wind mover
"""

import os
import math
from datetime import datetime, timedelta

import numpy as np

from gnome import scripting, initialize_log
from gnome.basic_types import datetime_value_2d

from gnome.utilities.projections import GeoProjection

from gnome.model import Model
from gnome.environment import Wind
from gnome.movers import RandomMover, PointWindMover
from gnome.spills import Spill
from gnome.spills.release import release_from_splot_data
from gnome.maps import MapFromBNA    # will be used once we have BNA map

from gnome.outputters import Renderer
from gnome.outputters import NetCDFOutput

# define base directory
base_dir = os.path.dirname(__file__)


def make_model(images_dir):
    print('initializing the model')

    start_time = datetime(2012, 9, 15, 12, 0)
    time_step = timedelta(minutes=15)
    duration = timedelta(hours=18)
    model = Model(time_step=time_step, start_time=start_time, duration=duration)

    # timeseries for wind data. The value is interpolated if time is between
    # the given datapoints
    series = np.zeros((4, ), dtype=datetime_value_2d)
    series[:] = [(start_time, (5, 180)),
                 (start_time + timedelta(hours=6), (10, 180)),
                 (start_time + timedelta(hours=12), (12, 180)),
                 (start_time + timedelta(hours=18), (8, 180))]
    wind = Wind(timeseries=series, units='m/s')
    model.environment += wind

    # include a wind mover and random diffusion
    print('adding movers')
    model.movers += [PointWindMover(wind), RandomMover()]

    # add particles
    print('adding particles')
    release = release_from_splot_data(start_time, 'GL.2013267._LE_WHOLELAKE.txt')
    model.spills += Spill(release=release)

    # output data as png images and in netcdf format
    print('adding outputters')
    netcdf_file = os.path.join(base_dir, 'script_example.nc')

    # DPZ
    lon = release.custom_positions[:,0]
    lat = release.custom_positions[:,1]
    xmn = math.floor(np.min(lon) - 0.5)
    xmx = math.ceil( np.max(lon) + 0.5)
    ymn = math.floor(np.min(lat) - 0.5)
    ymx = math.ceil( np.max(lat) + 0.5)
    viewport = ((xmn, ymn), (xmx, ymx))
    model.outputters += [Renderer(output_dir=images_dir, size=(800, 800), projection_class=GeoProjection, viewport=viewport),
                         NetCDFOutput(netcdf_file)]

    print('model complete')
    return model


if __name__ == '__main__':
    initialize_log(os.path.join(base_dir, 'log_config.json'))
    images_dir = os.path.join(base_dir, 'images')
    scripting.make_images_dir(images_dir)
    model = make_model(images_dir)
    model.full_run()
