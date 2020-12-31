# AUTO GENERATED FILE - DO NOT EDIT

from dash.development.base_component import Component, _explicitize_args


class Burger(Component):
    """A Burger component.
A modified version of dcc.Link that adds a few more options. E.g. you can disable scrolling to
the top upon updating the url.

Keyword arguments:
- children (a list of or a singular dash component, string or number; optional)
- style (dict; optional)
- id (string; optional)
- width (string; default "300px")
- height (string; default "100%")
- position (default "right")
- effect (default "slide")
- className (string; optional)
- overlay (boolean; default True)"""
    @_explicitize_args
    def __init__(self, children=None, style=Component.UNDEFINED, id=Component.UNDEFINED, width=Component.UNDEFINED, height=Component.UNDEFINED, position=Component.UNDEFINED, effect=Component.UNDEFINED, className=Component.UNDEFINED, overlay=Component.UNDEFINED, **kwargs):
        self._prop_names = ['children', 'style', 'id', 'width', 'height', 'position', 'effect', 'className', 'overlay']
        self._type = 'Burger'
        self._namespace = 'dash_extensions'
        self._valid_wildcard_attributes =            []
        self.available_properties = ['children', 'style', 'id', 'width', 'height', 'position', 'effect', 'className', 'overlay']
        self.available_wildcard_properties =            []

        _explicit_args = kwargs.pop('_explicit_args')
        _locals = locals()
        _locals.update(kwargs)  # For wildcard attrs
        args = {k: _locals[k] for k in _explicit_args if k != 'children'}

        for k in []:
            if k not in args:
                raise TypeError(
                    'Required argument `' + k + '` was not specified.')
        super(Burger, self).__init__(children=children, **args)
