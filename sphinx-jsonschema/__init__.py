# -*- coding: utf-8 -*-

"""
    sphinx-jsonschema
    -----------------

    This package adds the *jsonschema* directive to Sphinx.

    Using this directory you can render JSON Schema directly
    in Sphinx.

    :copyright: Copyright 2017-2020, Leo Noordergraaf
    :licence: GPL v3, see LICENCE for details.
"""

import csv
import os.path
import json
from jsonpointer import resolve_pointer
from traceback import format_exception, format_exception_only
import yaml
from collections import OrderedDict

from docutils import nodes, utils
from docutils.parsers.rst import Directive, DirectiveError
from docutils.parsers.rst import directives
from docutils.utils import SystemMessagePropagation
from docutils.utils.error_reporting import SafeString
from .wide_format import WideFormat


def pairwise(seq):
    """ Iterate over pairs in a sequence """
    return zip(seq, seq[1:])


def maybe_int(val):
    """ Convert value to an int and return it or just return the value """
    try:
        return int(val)
    except:
        return val


def json_path_validate(path):
    """ Check that json path doesn't contain consecutive or trailing wildcards """
    if path[-1] in ('*', '**'):
        return False

    forbidden = {
        ('**', '**'),
        ('**', '*'),
        ('*', '**'),
        ('*', '*')
    }

    return set(pairwise(path)) & forbidden != set()


def _json_fan_out(doc, path, transform, deep=False):
    """ Process */** wildcards in the path by fanning out the processing to multiple keys """
    if not isinstance(doc, dict) or not path:
        return

    targets = []

    for (k,v) in doc.items():
        if k == path[0]:
            targets.append(k)
        else:
            if isinstance(v, dict):
                if deep:
                    _json_fan_out(v, path, transform, deep=True)
                else:
                    sub_path = path[1:]
                    if sub_path:
                        _json_bind(v, sub_path, transform)

    # Since transformers can mutate the original document
    # we need to process them once we're done iterating
    for r in targets:
        transform(doc, r)


def _json_bind(doc, path, transform):
    """ Bind (sub)document to a particular path """
    obj = doc
    last = obj

    for idx, p in enumerate(path):
        if p in ('**', '*'):
            return _json_fan_out(
                obj,
                path[idx+1:],
                transform,
                deep=(p == '**')
            )

        last = obj
        try:
            obj = obj[p]
        except (KeyError, IndexError):
            return

    if path:
        transform(last, path[-1])


def json_path_transform(document, path, transformer):
    """ Transform items in `document` conforming to `path` with a `transformer` in-place """

    # Try to cast parts of the path as int if possible so that we can support
    # paths like `/some/array/0/something` and we don't end up with TypeError
    # trying to index into a list with a string '0'
    parts = [maybe_int(p) for p in path.split('/')[1:]]

    if not parts or not json_path_validate(path):
        raise ValueError('Supplied JSON path is invalid')

    _json_bind(document, parts, transformer)


def remove(doc, key):
    del doc[key]


def remove_empty(doc, key):
    if not doc[key]:
        del doc[key]


def hide_key(item):
    if item:
        return list(csv.reader([item])).pop()
    raise ValueError('Invalid JSON path: "%s"' % item)


def flag(argument):
    if argument is None:
        return True

    value = argument.lower().strip()
    if value in ['on', 'true']:
        return True
    if value in ['off', 'false']:
        return False
    raise ValueError('"%s" unknown, choose from "On", "True", "Off" or "False"' % argument)

class JsonSchema(Directive):
    optional_arguments = 1
    has_content = True
    option_spec = {'lift_title': flag,
                   'lift_description': flag,
                   'lift_definitions': flag,
                   'auto_reference': flag,
                   'auto_target': flag,
                   'timeout': float,
                   'encoding': directives.encoding,
                   'hide_key': hide_key,
                   'hide_key_if_empty': hide_key}

    def run(self):
        try:
            schema, source, pointer = self.get_json_data()

            if self.options['hide_key']:
                for hide_path in self.options['hide_key']:
                    json_path_transform(schema, hide_path, remove)
            if self.options['hide_key_if_empty']:
                for hide_path in self.options['hide_key_if_empty']:
                    json_path_transform(schema, hide_path, remove_empty)

            format = WideFormat(self.state, self.lineno, source, self.options, self.state.document.settings.env.app)
            return format.run(schema, pointer)
        except SystemMessagePropagation as detail:
            return [detail.args[0]]
        except DirectiveError as error:
            raise self.directive_error(error.level, error.msg)
        except Exception as error:
            tb = error.__traceback__
            # loop through all traceback points to only return the last traceback
            while tb.tb_next:
                tb = tb.tb_next

            raise self.error(''.join(format_exception(type(error), error, tb, chain=False)))

    def get_json_data(self):
        """
        Get JSON data from the directive content, from an external
        file, or from a URL reference.
        """
        if self.arguments:
            filename, pointer = self._splitpointer(self.arguments[0])
        else:
            filename = None
            pointer = ''

        if self.content:
            schema, source = self.from_content(filename)
        elif filename and filename.startswith('http'):
            schema, source = self.from_url(filename)
        elif filename:
            schema, source = self.from_file(filename)
        else:
            raise self.error('"%s" directive has no content or a reference to an external file.'
                             % self.name)

        try:
            schema = self.ordered_load(schema)
        except Exception as error:
            error = self.state_machine.reporter.error(
                    '"%s" directive encountered a the following error while parsing the data.\n %s'
                     % (self.name, SafeString("".join(format_exception_only(type(error), error)))),
                    nodes.literal_block(schema, schema), line=self.lineno)
            raise SystemMessagePropagation(error)

        if pointer:
            try:
                schema = resolve_pointer(schema, pointer)
            except KeyError:
                error = self.state_machine.reporter.error(
                    '"%s" directive encountered a KeyError when trying to resolve the pointer'
                    ' in schema: %s' % (self.name, SafeString(pointer)),
                    nodes.literal_block(schema, schema), line=self.lineno)
                raise SystemMessagePropagation(error)

        return schema, source, pointer

    def from_content(self, filename):
        if filename:
            error = self.state_machine.reporter.error(
                '"%s" directive may not both specify an external file and'
                ' have content.' % self.name,
                nodes.literal_block(self.block_text, self.block_text),
                line=self.lineno)
            raise SystemMessagePropagation(error)

        source = self.content.source(0)
        data = '\n'.join(self.content)
        return data, source

    def from_url(self, url):
        # To prevent loading on a not existing adress added timeout
        timeout = self.options.get('timeout', 30)
        if timeout < 0:
            timeout = None

        try:
            import requests
        except ImportError:
            raise self.error('"%s" directive requires requests when loading from http.'
                             ' Try "pip install requests".' % self.name)

        try:
            response = requests.get(url, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise self.error(u'"%s" directive recieved an "%s" when loading from url: %s.'
                                % (self.name, type(e), url))

        if response.status_code != 200:
            # When making a connection to the url a status code will be returned
            # Normally a OK (200) response would we be returned all other responses
            # an error will be raised could be separated futher
            raise self.error(u'"%s" directive received an "%s" when loading from url: %s.'
                                % (self.name, response.reason, url))

        # response content always binary converting with decode() no specific format defined
        data = response.content.decode()
        return data, url

    def from_file(self, filename):
        document_source = os.path.dirname(self.state.document.current_source)
        if not os.path.isabs(filename):
            # file relative to the path of the current rst file
            source = os.path.join(document_source, filename)
        else:
            source = filename

        try:
            with open(source, encoding=self.options.get('encoding')) as file:
                data = file.read()
        except IOError as error:
            raise self.error(u'"%s" directive encountered an IOError while loading file: %s\n%s'
                                % (self.name, source, error))

        # Simplifing source path and to the document a new dependency
        source = utils.relative_path(document_source, source)
        self.state.document.settings.record_dependencies.add(source)

        return data, source

    def _splitpointer(self, path):
        val = path.rsplit('#', 1)
        if len(val) == 1:
            val.append('')
        return val

    def ordered_load(self, text, Loader=yaml.SafeLoader, object_pairs_hook=OrderedDict):
        """Allows you to use `pyyaml` to load as OrderedDict.

        Taken from https://stackoverflow.com/a/21912744/1927102
        """
        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))

        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping)

        text = text.replace('\\(', '\\\\(')
        text = text.replace('\\)', '\\\\)')
        try:
            result = yaml.load(text, OrderedLoader)
        except yaml.scanner.ScannerError:
            # will it load as plain json?
            result = json.loads(text, object_pairs_hook=object_pairs_hook)
        return result


def setup(app):
    app.add_directive('jsonschema', JsonSchema)
    app.add_config_value('jsonschema_options', {}, 'env')
    return {
        'parallel_read_safe': True,
        'version': '1.17.0'
    }
