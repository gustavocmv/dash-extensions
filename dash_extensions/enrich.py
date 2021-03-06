import functools
import hashlib
import json
import pickle
import secrets
import uuid
import dash
import dash_html_components as html
import dash.dependencies as dd
import plotly

from dash.dependencies import Input, State, Output, MATCH, ALL, ALLSMALLER, _Wildcard
from dash.exceptions import PreventUpdate
from flask import session
from flask_caching.backends import FileSystemCache
from more_itertools import unique_everseen, flatten
from json.decoder import JSONDecodeError

_wildcard_mappings = {ALL: "<ALL>", MATCH: "<MATCH>", ALLSMALLER: "<ALLSMALLER>"}
_wildcard_values = list(_wildcard_mappings.values())


# region Dash proxy


class DashProxy(dash.Dash):

    def __init__(self, *args, transforms=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.callbacks = []
        self.arg_types = [dd.Output, dd.Input, dd.State]
        self.transforms = transforms if transforms is not None else []
        # Do the transform initialization.
        for transform in self.transforms:
            transform.init(self)

    def callback(self, *args, **kwargs):
        """
         This method saves the callbacks on the DashTransformer object. It acts as a proxy for the Dash app callback.
        """
        # Parse Output/Input/State (could be made simpler by enforcing input structure)
        keys = ['output', 'inputs', 'state']
        args = list(args) + list(flatten([_extract_list_from_kwargs(kwargs, key) for key in keys]))
        callback = {arg_type: [] for arg_type in self.arg_types}
        arg_order = []
        multi_output = False
        for arg in args:
            elements = _as_list(arg)
            for element in elements:
                for key in callback:
                    if isinstance(element, key):
                        # Check if this is a wild card output.
                        if not multi_output and isinstance(element, dd.Output):
                            component_id = element.component_id
                            if isinstance(component_id, dict):
                                multi_output = any([component_id[k] in [dd.ALLSMALLER, dd.ALL] for k in component_id])
                        callback[key].append(element)
                        arg_order.append(element)
        if not multi_output:
            multi_output = len(callback[dd.Output]) > 1
        # Save the kwargs for later.
        callback["kwargs"] = kwargs
        callback["sorted_args"] = arg_order
        callback["multi_output"] = multi_output
        # Save the callback for later.
        self.callbacks.append(callback)

        def wrapper(f):
            callback["f"] = f

        return wrapper

    def _register_callbacks(self, app=None):
        callbacks = list(self._resolve_callbacks())
        for callback in callbacks:
            outputs = callback[dd.Output][0] if len(callback[dd.Output]) == 1 else callback[dd.Output]
            if app is None:
                super().callback(outputs, callback[dd.Input], callback[dd.State], **callback["kwargs"])(callback["f"])
            else:
                app.callback(outputs, callback[dd.Input], callback[dd.State], **callback["kwargs"])(callback["f"])

    def _layout_value(self):
        layout = self._layout() if self._layout_is_function else self._layout
        for transform in self.transforms:
            layout = transform.layout(layout, self._layout_is_function)
        return layout

    def _setup_server(self):
        """
         This method registers the callbacks on the Dash app and injects a session secret.
        """
        # Register the callbacks.
        self._register_callbacks()
        # Proceed as normally.
        super()._setup_server()
        # Set session secret. Used by some subclasses.
        if not self.server.secret_key:
            self.server.secret_key = secrets.token_urlsafe(16)

    def _resolve_callbacks(self):
        """
         This method resolves the callbacks, i.e. it applies the callback injections.
        """
        callbacks = self.callbacks
        for transform in self.transforms:
            callbacks = transform.apply(callbacks)
        return callbacks


def _get_session_id(session_key=None):
    session_key = "session_id" if session_key is None else session_key
    # Create unique session id.
    if not session.get(session_key):
        session[session_key] = secrets.token_urlsafe(16)
    return session.get(session_key)


def _as_list(item):
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def _create_callback_id(item):
    cid = item.component_id
    if isinstance(cid, dict):
        cid = {key: cid[key] if cid[key] not in _wildcard_mappings else _wildcard_mappings[cid[key]] for key in cid}
        cid = json.dumps(cid)
    return "{}.{}".format(cid, item.component_property)


def _extract_list_from_kwargs(kwargs: dict, key: str) -> list:
    if kwargs is not None and key in kwargs:
        contents = kwargs.pop(key)
        if contents is None:
            return []
        if isinstance(contents, list):
            return contents
        else:
            return [contents]
    else:
        return []


def plotly_jsonify(data):
    return json.loads(json.dumps(data, cls=plotly.utils.PlotlyJSONEncoder))


class DashTransform:

    def init(self, dt):
        pass

    def apply(self, callbacks):
        raise NotImplementedError()

    def layout(self, layout, layout_is_function):
        return layout


# endregion

# region Prefix ID transform

class PrefixIdTransform(DashTransform):

    def __init__(self, prefix):
        self.prefix = prefix
        self.initialized = False

    def apply(self, callbacks):
        for callback in callbacks:
            for arg in callback["sorted_args"]:
                arg.component_id = apply_prefix(self.prefix, arg.component_id)
        return callbacks

    def layout(self, layout, layout_is_function):
        # TODO: Will this work with layout functions?
        if layout_is_function or not self.initialized:
            prefix_id_recursively(layout, self.prefix)
            self.initialized = True
        return layout


def apply_prefix(prefix, component_id):
    if isinstance(component_id, dict):
        for key in component_id:
            # This branch handles the IDs. TODO: Can we always assume use of ints?
            if type(component_id[key]) == int:
                continue
            # This branch handles the wildcard callbacks.
            if isinstance(component_id[key], _Wildcard):
                continue
            # All "normal" props are prefixed.
            component_id[key] = "{}-{}".format(prefix, component_id[key])
        return component_id
    return "{}-{}".format(prefix, component_id)


def prefix_id_recursively(item, key):
    if hasattr(item, "id"):
        item.id = apply_prefix(key, item.id)
    if hasattr(item, "children"):
        children = _as_list(item.children)
        for child in children:
            prefix_id_recursively(child, key)


# endregion

# region Trigger transform (the only default transform)

class Trigger(Input):
    """
     Like an Input, a trigger can trigger a callback, but it's values it not included in the resulting function call.
    """

    def __init__(self, component_id, component_property):
        super().__init__(component_id, component_property)


class TriggerTransform(DashTransform):

    def apply(self, callbacks):
        for callback in callbacks:
            is_trigger = trigger_filter(callback["sorted_args"])
            # Check if any triggers are there.
            if not any(is_trigger):
                continue
            # If so, filter the callback args.
            f = callback["f"]
            callback["f"] = filter_args(is_trigger)(f)
        return callbacks


def filter_args(args_filter):
    def wrapper(f):
        @functools.wraps(f)
        def decorated_function(*args):
            filtered_args = [arg for j, arg in enumerate(args) if not args_filter[j]]
            return f(*filtered_args)

        return decorated_function

    return wrapper


def trigger_filter(args):
    inputs_args = [item for item in args if isinstance(item, dd.Input) or isinstance(item, dd.State)]
    is_trigger = [isinstance(item, Trigger) for item in inputs_args]
    return is_trigger


# endregion

# region Group transform

class GroupTransform(DashTransform):

    def apply(self, callbacks):
        groups = {}
        # Figure out which callbacks to group together.
        grouped_callbacks = []
        for i in range(len(callbacks)):
            key = callbacks[i]["kwargs"].pop("group", None)
            if key:
                if key not in groups:
                    groups[key] = []
                groups[key].append(i)
            else:
                grouped_callbacks.append(callbacks[i])
        # Do the grouping.
        for key in groups:
            grouped_callback = _combine_callbacks([callbacks[i] for i in groups[key]])
            grouped_callbacks.append(grouped_callback)

        return grouped_callbacks


# NOTE: No performance considerations what so ever. Just an initial proof-of-concept implementation.
def _combine_callbacks(callbacks):
    inputs, input_props, input_prop_lists, input_mappings = _prep_props(callbacks, dd.Input)
    states, state_props, state_prop_lists, state_mappings = _prep_props(callbacks, dd.State)
    outputs, output_props, output_prop_lists, output_mappings = _prep_props(callbacks, dd.Output)
    # TODO: What kwargs to use?
    kwargs = callbacks[0]["kwargs"]
    multi_output = any([callback["multi_output"] for callback in callbacks])
    if not multi_output:
        all_outputs = []
        for callback in callbacks:
            all_outputs += callback[dd.Output]
        multi_output = len(list(set(all_outputs))) > 1

    # TODO: There might be a scope issue here
    def wrapper(*args):
        local_inputs = list(args)[:len(inputs)]
        local_states = list(args)[len(inputs):]
        if len(dash.callback_context.triggered) == 0:
            raise PreventUpdate
        prop_id = dash.callback_context.triggered[0]['prop_id']
        output_values = [dash.no_update] * len(outputs)
        for i, entry in enumerate(input_prop_lists):
            # Check if the trigger is an input of the callback.
            match = False
            for item in entry:
                # Check for exact matches.
                match = item == prop_id
                if match:
                    break
                # Check for wild card matches.
                if any([wildcard_value in item for wildcard_value in _wildcard_values]):
                    try:
                        props = json.loads(prop_id.split(".")[0])
                        item_props = json.loads(item.split(".")[0])
                        prop_match = True
                        for key in props:
                            if item_props[key] not in _wildcard_values:
                                prop_match = prop_match and item_props[key] == props[key]
                            # TODO: Make checks here, no checks (as now) is only valid for ALL
                        if prop_match:
                            match = True
                            break
                    except JSONDecodeError:
                        continue
            if not match:
                continue
            # Trigger the callback function.
            try:
                inputs_i = [local_inputs[j] for j in input_mappings[i]]
                states_i = [local_states[j] for j in state_mappings[i]]
                outputs_i = callbacks[i]["f"](*inputs_i, *states_i)
                if not callbacks[i]["multi_output"]:  # len(callbacks[i][Output]) == 1:  TODO: Is this right?
                    outputs_i = [outputs_i]
                for j, item in enumerate(outputs_i):
                    output_values[output_mappings[i][j]] = outputs_i[j]
            except PreventUpdate:
                continue
        # Check if an update is needed.
        if all([item == dash.no_update for item in output_values]):
            raise PreventUpdate
        # Return the combined output.
        return output_values if multi_output else output_values[0]  # TODO: Check for multi output here?

    return {dd.Output: outputs, dd.Input: inputs, "sorted_args": outputs + inputs + states,
            "f": wrapper, dd.State: states, "kwargs": kwargs, "multi_output": multi_output}


def _prep_props(callbacks, key):
    all = []
    for callback in callbacks:
        all.extend(callback[key])
    all = list(unique_everseen(all))
    props = [_create_callback_id(item) for item in all]
    prop_lists = [[_create_callback_id(item) for item in callback[key]] for callback in callbacks]
    mappings = [[props.index(item) for item in l] for l in prop_lists]
    return all, props, prop_lists, mappings


# endregion

# region Server side output transform
DEFAULT_CACHE_ARGS = ['output', 'func', 'args', 'session']


class EnrichedOutput(Output):
    """
     Like a normal Output, includes additional properties related to storing the data.
    """

    def __init__(self, component_id, component_property, backend=None, cache_args=None):
        super().__init__(component_id, component_property)
        self.backend = backend
        self.cache_args = cache_args


class ServersideOutput(EnrichedOutput):
    """
     Like a normal Output, but with the content stored only server side.
    """


class ServersideOutputTransform(DashTransform):

    def __init__(self, backend=None, cache_args=DEFAULT_CACHE_ARGS):
        self.backend = backend if backend is not None else FileSystemStore()
        self.cache_args = cache_args

    def init(self, dt):
        # Set session secret (if not already set).
        if not dt.server.secret_key:
            dt.server.secret_key = secrets.token_urlsafe(16)

    def apply(self, callbacks):
        # 1) Creat index.
        serverside_callbacks = []
        serverside_output_map = {}
        for callback in callbacks:
            # If memoize keyword is used, serverside caching is needed.
            memoize = callback["kwargs"].get("memoize", None)
            serverside = False
            # Keep tract of which outputs are server side outputs.
            for output in callback[dd.Output]:
                if isinstance(output, ServersideOutput):
                    serverside_output_map[_create_callback_id(output)] = output
                    serverside = True
                # Set default values.
                if not isinstance(output, ServersideOutput) and not memoize:
                    continue
                if output.backend is None:
                    output.backend = self.backend
                if output.cache_args is None:
                    output.cache_args = self.cache_args
            # Keep track of server side callbacks.
            if serverside or memoize:
                serverside_callbacks.append(callback)
        # 2) Inject cached data into callbacks.
        for callback in callbacks:
            # Figure out which args need loading.
            items = callback[dd.Input] + callback[dd.State]
            item_ids = [_create_callback_id(item) for item in items]
            serverside_outputs = [serverside_output_map.get(item_id, None) for item_id in item_ids]
            # If any arguments are packed, unpack them.
            if any(serverside_outputs):
                f = callback["f"]
                callback["f"] = _unpack_outputs(serverside_outputs)(f)
        # 3) Apply the caching itself.
        for i, callback in enumerate(serverside_callbacks):
            f = callback["f"]
            callback["f"] = _pack_outputs(callback)(f)
        # 4) Strip special args.
        for callback in callbacks:
            for key in ["memoize"]:
                callback["kwargs"].pop(key, None)

        return callbacks


def _unpack_outputs(serverside_outputs):
    def unpack(f):
        @functools.wraps(f)
        def decorated_function(*args):
            if not any(serverside_outputs):
                return f(*args)
            args = list(args)
            for i, serverside_output in enumerate(serverside_outputs):
                # Just skip elements that are not stored server side.
                if not serverside_output:
                    continue
                # Replace content of element(s).
                try:
                    args[i] = serverside_output.backend.get(args[i], ignore_expired=True)
                except TypeError as ex:
                    # TODO: Should we do anything about this?
                    args[i] = None
            return f(*args)

        return decorated_function

    return unpack


def _pack_outputs(callback):
    memoize = callback["kwargs"].get("memoize", None)

    def packed_callback(f):
        @functools.wraps(f)
        def decorated_function(*args):
            multi_output = callback["multi_output"]
            # If memoize is enabled, we check if the cache already has a valid value.
            if memoize:
                # Figure out if an update is necessary.
                unique_ids = []
                update_needed = False
                for i, output in enumerate(callback[Output]):
                    # Filter out Triggers (a little ugly to do here, should ideally be handled elsewhere).
                    is_trigger = trigger_filter(callback["sorted_args"])
                    filtered_args = [arg for i, arg in enumerate(args) if not is_trigger[i]]
                    # Generate unique ID.
                    unique_id = _get_cache_id(f, output, list(filtered_args), output.cache_args)
                    unique_ids.append(unique_id)
                    if not output.backend.has(unique_id):
                        update_needed = True
                        break
                # If not update is needed, just return the ids (or values, if not serverside output).
                if not update_needed:
                    results = [uid if isinstance(callback[Output][i], ServersideOutput) else
                               callback[Output][i].backend.get(uid) for i, uid in enumerate(unique_ids)]
                    return results if multi_output else results[0]
            # Do the update.
            data = f(*args)
            data = list(data) if multi_output else [data]
            if callable(memoize):
                data = memoize(data)
            for i, output in enumerate(callback[Output]):
                serverside_output = isinstance(callback[Output][i], ServersideOutput)
                # Replace only for server side outputs.
                if serverside_output or memoize:
                    # Filter out Triggers (a little ugly to do here, should ideally be handled elsewhere).
                    is_trigger = trigger_filter(callback["sorted_args"])
                    filtered_args = [arg for i, arg in enumerate(args) if not is_trigger[i]]
                    unique_id = _get_cache_id(f, output, list(filtered_args), output.cache_args)
                    output.backend.set(unique_id, data[i])
                    # Replace only for server side outputs.
                    if serverside_output:
                        data[i] = unique_id
            return data if multi_output else data[0]

        return decorated_function

    return packed_callback


def _get_cache_id(func, output, args, cache_args=DEFAULT_CACHE_ARGS):  
    all_args = []
    if 'func' in cache_args:
        all_args.append(func.__name__)
    if 'output' in cache_args:
        all_args.append(_create_callback_id(output))
    if 'args' in cache_args:
        all_args += list(args)
    if 'session' in cache_args:
        all_args.append(_get_session_id())

    cache_id = hashlib.md5(json.dumps(all_args).encode()).hexdigest()

    return cache_id


# Interface definition for server stores.

class ServerStore:

    def get(self, key, ignore_expired=False):
        raise NotImplementedError()

    def set(self, key, value):
        raise NotImplementedError()

    def has(self, key):
        raise NotImplementedError()


# Place store implementations here.

class FileSystemStore(FileSystemCache):

    def __init__(self, cache_dir="file_system_store", **kwargs):
        super().__init__(cache_dir, **kwargs)

    def get(self, key, ignore_expired=False):
        if not ignore_expired:
            return super().get(key)
        # TODO: This part must be implemented for each type of cache.
        filename = self._get_filename(key)
        try:
            with open(filename, "rb") as f:
                pickle_time = pickle.load(f)  # ignore time
                return pickle.load(f)
        except (IOError, OSError, pickle.PickleError):
            return None


# endregion

# region No output transform

class NoOutputTransform(DashTransform):

    def __init__(self):
        self.initialized = False
        self.hidden_divs = []

    def layout(self, layout, layout_is_function):
        if layout_is_function or not self.initialized:
            children = _as_list(layout.children) + self.hidden_divs
            layout.children = children
            self.initialized = True
        return layout

    def apply(self, callbacks):
        for callback in callbacks:
            if len(callback[dd.Output]) == 0:
                output_id = str(uuid.uuid4())
                hidden_div = html.Div(id=output_id, style={"display": "none"})
                callback[dd.Output] = [dd.Output(output_id, "children")]
                self.hidden_divs.append(hidden_div)
        return callbacks


# endregion

# region Transformer implementations

class Dash(DashProxy):
    def __init__(self, *args, output_defaults=dict(backend=None, cache_args=DEFAULT_CACHE_ARGS), **kwargs):
        transforms = [TriggerTransform(), NoOutputTransform(), GroupTransform(),
                      ServersideOutputTransform(**output_defaults)]
        super().__init__(*args, transforms=transforms, **kwargs)

# endregion
