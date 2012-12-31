"""
util.py: Utility function for the webgnome package.
"""
import datetime
import inspect
import math
import uuid

from functools import wraps
from itertools import chain
from pyramid.renderers import JSON
from hazpy.unit_conversion.unit_data import ConvertDataUnits


def make_message(type, text):
    """
    Create a dictionary suitable to be returned in a JSON response as a
    "message" sent to the JavaScript client.

    The client looks for "message" objects included in JSON responses that the
    server sends back on successful form submits and if one is present, and has
    a ``type`` field and ``text`` field, it will display the message to the
    user.
    """
    return dict(type=type, text=text)


def encode_json_date(obj):
    """
    Render a :class:`datetime.datetime` or :class:`datetime.date` object using
    the :meth:`datetime.isoformat` function, so it can be properly serialized to
    JSON notation.
    """
    if isinstance(obj, datetime.datetime) or isinstance(obj, datetime.date):
        return obj.isoformat()


def json_encoder(obj):
    """
    A custom JSON encoder that handles :class:`datetime.datetime` and
    :class:`datetime.date` values, with a fallback to using the :func:`str`
    representation of an object.
    """
    date_str = encode_json_date(obj)

    if date_str:
        return date_str
    else:
        return str(obj)


def json_date_adapter(obj, request):
    """
    A wrapper around :func:`json_date_encoder` so that it may be used in a
    custom JSON adapter for a Pyramid renderer.
    """
    return encode_json_date(obj)


gnome_json = JSON(adapters=(
    (datetime.datetime, json_date_adapter),
    (datetime.date, json_date_adapter),
    (uuid.UUID, lambda obj, request: str(obj))
))


def get_model_from_request(request):
    """
    Return a :class:`gnome.model.Model` if the user has a session key that
    matches the ID of a running model.
    """
    settings = request.registry.settings
    model_id = request.session.get(settings.model_session_key, None)

    try:
        model = settings.Model.get(model_id)
    except settings.Model.DoesNotExist:
        model = None

    return model


def get_form_route(request, obj, route_type):
    """
    Find a route name for ``obj`` given the type of route.

    ``route_type`` is a short-hand description like "create" or "delete" used
    as a key in the ``form_routes`` dictionary.
    """
    route = None
    form_cls = get_obj_class(obj)
    routes = request.registry.settings['form_routes'].get(form_cls, None)

    if routes:
        route = routes.get(route_type, None)

    return route


MISSING_MODEL_ERROR = {
    'error': True,
    'message': make_message('error', 'That model is no longer available.')
}


def valid_model_id(request):
    """
    A Cornice validator that returns a 404 if a valid model was not found
    in the user's session.
    """
    model = get_model_from_request(request)

    if model is None:
        request.errors.add('body', 'model', 'Model not found.')
        request.errors.status = 404
        return


def valid_mover_id(request):
    """
    A Cornice validator that returns a 404 if a valid mover was not found using
    an ``id`` matchdict value.
    """
    valid_model_id(request)
    model = get_model_from_request(request)

    if request.errors:
        return

    mover_exists = model.has_mover(int(request.matchdict['id']))

    if not mover_exists:
        request.errors.add('body', 'mover', 'Mover not found.')
        request.errors.status = 404


def valid_spill_id(request):
    """
    A Cornice validator that returns a 404 if a valid spill was not found using
    an ``id`` matchdict value.
    """
    valid_model_id(request)
    model = get_model_from_request(request)

    if request.errors:
        return

    spill_exists = model.has_spill(int(request.matchdict['id']))

    if not spill_exists:
        request.errors.add('body', 'spill', 'Spill not found.')
        request.errors.status = 404


def require_model(f):
    """
    Wrap a JSON view in a precondition that checks if the user has a valid
    ``model_id`` in his or her session and fails if not.

    If the key is missing or no model is found for that key, create a new model.

    This decorator works on functions and methods. It returns a method decorator
    if the first argument to the function is ``self``. Otherwise, it returns a
    function decorator.

    Instead of returning an error, should we just create a model?
    """
    args = inspect.getargspec(f)

    if args and args.args[0] == 'self':
        @wraps(f)
        def inner_method(self, *args, **kwargs):
            model = get_model_from_request(self.request)
            if model is None:
                model = self.request.registry.settings.Model.create()
            return f(self, model, *args, **kwargs)
        wrapper = inner_method
    else:
        @wraps(f)
        def inner_fn(request, *args, **kwargs):
            model = get_model_from_request(request)
            if model is None:
                model = request.registry.settings.Model.create()
            return f(request, model, *args, **kwargs)
        wrapper = inner_fn
    return wrapper


def get_obj_class(obj):
    return obj if type(obj) == type else obj.__class__


class DirectionConverter(object):
    DIRECTIONS = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW"
    ]

    @classmethod
    def is_cardinal_direction(cls, direction):
        return direction in cls.DIRECTIONS

    @classmethod
    def get_cardinal_name(cls, degree):
        """
        Convert an integer degree into a cardinal direction name.
        """
        idx = int(math.floor((+(degree) + 360 / 32) / (360 / 16) % 16))
        if idx:
            return cls.DIRECTIONS[idx]

    @classmethod
    def get_degree(cls, cardinal_direction):
        """
       Convert a cardinal direction name into an integer degree.
       """
        idx = cls.DIRECTIONS.index(cardinal_direction.upper())
        if idx:
            return (360.0 / 16) * idx


velocity_unit_values = list(chain.from_iterable(
    [item[1] for item in ConvertDataUnits['Velocity'].values()]))

velocity_unit_options = [(values[1][0], values[1][0]) for label, values in
                         ConvertDataUnits['Velocity'].items()]
