# coding=utf-8
"""
terminal.common.utils
~~~~~~~~~~~~~~~~~~~~~

本模块提供项目工具库

"""
import base64
import binascii
import contextlib
import functools
import logging
import os.path
import shutil
import tempfile
import time
import hashlib
from Crypto.Cipher import AES

import requests
from talos.core import config
from talos.core import utils
from talos.core import exceptions as base_ex
from talos.core.i18n import _
from talos.utils import scoped_globals
from terminal.common import exceptions

try:
    HAS_FCNTL = True
    import fcntl
except:
    HAS_FCNTL = False

LOG = logging.getLogger(__name__)
CONF = config.CONF

# register jar,war,apk as zip file
shutil.register_unpack_format('jar', ['.jar'], shutil._UNPACK_FORMATS['zip'][1])
shutil.register_unpack_format('war', ['.war'], shutil._UNPACK_FORMATS['zip'][1])
shutil.register_unpack_format('apk', ['.apk'], shutil._UNPACK_FORMATS['zip'][1])


def unpack_file(filename, unpack_dest):
    shutil.unpack_archive(filename, unpack_dest)


@contextlib.contextmanager
def lock(name, block=True, timeout=5):
    timeout = 1.0 * timeout
    if HAS_FCNTL:
        acquired = False
        filepath = os.path.join(tempfile.gettempdir(), 'artifacts_lock_%s' % name)
        fp = open(filepath, "a+")
        flag = fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            if not block:
                # non-block yield immediately
                try:
                    fcntl.flock(fp, flag)
                    acquired = True
                except:
                    pass
                yield acquired
            else:
                # block will try until timeout
                time_pass = 0
                while time_pass < timeout:
                    try:
                        fcntl.flock(fp, flag)
                        acquired = True
                        break
                    except:
                        gap = 0.1
                        time.sleep(gap)
                        time_pass += gap
                yield acquired
        finally:
            if acquired:
                fcntl.flock(fp, fcntl.LOCK_UN)
            fp.close()
    else:
        yield False


def json_or_error(func):
    @functools.wraps(func)
    def _json_or_error(url, **kwargs):
        try:
            try:
                return func(url, **kwargs)
            except requests.ConnectionError as e:
                LOG.error('http error: %s %s, reason: %s', func.__name__.upper(), url, str(e))
                raise base_ex.CallBackError(message={
                    'code': 50002,
                    'title': _('Connection Error'),
                    'description': _('Failed to establish a new connection')
                })
            except requests.Timeout as e:
                LOG.error('http error: %s %s, reason: %s', func.__name__.upper(), url, str(e))
                raise base_ex.CallBackError(message={
                    'code': 50004,
                    'title': _('Timeout Error'),
                    'description': _('Server do not respond')
                })
            except requests.HTTPError as e:
                LOG.error('http error: %s %s, reason: %s', func.__name__.upper(), url, str(e))
                code = int(e.response.status_code)
                message = RestfulJson.get_response_json(e.response, default={'code': code})
                if code == 401:
                    raise base_ex.AuthError()
                if code == 404:
                    message['title'] = _('Not Found')
                    message['description'] = _('The resource you request not exist')
                # 如果后台返回的数据不符合要求，强行修正
                if 'code' not in message:
                    message['code'] = code
                if 'title' not in message:
                    message['title'] = message.get('title', e.response.reason)
                if 'description' not in message:
                    message['description'] = message.get('message', str(e))
                raise base_ex.CallBackError(message=message)
            except Exception as e:
                LOG.error('http error: %s %s, reason: %s', func.__name__.upper(), url, str(e))
                message = RestfulJson.get_response_json(e.response,
                                                        default={
                                                            'code': 500,
                                                            'title': _('Server Error'),
                                                            'description': str(e)
                                                        })
                if 'code' not in message:
                    message['code'] = 500
                if 'title' not in message:
                    message['title'] = message.get('title', str(e))
                if 'description' not in message:
                    message['description'] = message.get('message', str(e))
                raise base_ex.CallBackError(message=message)
        except base_ex.CallBackError as e:
            raise exceptions.PluginError(message=e.message, error_code=e.code)

    return _json_or_error


class RestfulJson(object):
    @staticmethod
    def get_response_json(resp, default=None):
        try:
            return resp.json()
        except Exception as e:
            return default

    @staticmethod
    @json_or_error
    def post(url, **kwargs):
        resp = requests.post(url, **kwargs)
        resp.raise_for_status()
        return RestfulJson.get_response_json(resp)

    @staticmethod
    @json_or_error
    def get(url, **kwargs):
        resp = requests.get(url, **kwargs)
        resp.raise_for_status()
        return RestfulJson.get_response_json(resp)

    @staticmethod
    @json_or_error
    def patch(url, **kwargs):
        resp = requests.patch(url, **kwargs)
        resp.raise_for_status()
        return RestfulJson.get_response_json(resp)

    @staticmethod
    @json_or_error
    def delete(url, **kwargs):
        resp = requests.delete(url, **kwargs)
        resp.raise_for_status()
        return RestfulJson.get_response_json(resp)

    @staticmethod
    @json_or_error
    def put(url, **kwargs):
        resp = requests.put(url, **kwargs)
        resp.raise_for_status()
        return RestfulJson.get_response_json(resp)


def b64decode_key(key):
    new_key = key
    max_padding = 3
    while max_padding > 0:
        try:
            return base64.b64decode(new_key)
        except binascii.Error as e:
            new_key += '='
            max_padding -= 1
            if max_padding <= 0:
                raise e


def get_token():
    return utils.get_attr(scoped_globals.GLOBALS, 'request.auth_token') or CONF.wecube.token


class ClientMixin:
    # mixin need self.token & self.server
    def build_headers(self):
        headers = None
        if self.token:
            headers = {'Authorization': 'Bearer ' + self.token}
        return headers

    def build_url(self, url_path):
        return self.server + url_path

    def check_response(self, resp_json):
        if resp_json['status'] != 'OK':
            # 当创建/更新条目错误，且仅有一个错误时，返回内部错误信息
            if isinstance(resp_json.get('data', None), list) and len(resp_json['data']) == 1:
                if 'message' in resp_json['data'][0]:
                    raise exceptions.PluginError(message=resp_json['data'][0]['message'])
            raise exceptions.PluginError(message=resp_json['message'])

    def get(self, url, param=None):
        LOG.info('GET %s', url)
        LOG.debug('Request: query - %s, data - None', str(param))
        resp_json = RestfulJson.get(url, headers=self.build_headers(), params=param)
        LOG.debug('Response: %s', str(resp_json))
        self.check_response(resp_json)
        return resp_json

    def post(self, url, data, param=None):
        LOG.info('POST %s', url)
        LOG.debug('Request: query - %s, data - %s', str(param), str(data))
        resp_json = RestfulJson.post(url, headers=self.build_headers(), params=param, json=data)
        LOG.debug('Response: %s', str(resp_json))
        self.check_response(resp_json)
        return resp_json


# def transform_filter_to_cmdb_query(filters, orders=None, offset=None, limit=None, fields_mapping=None):
#     '''transform filter from talos filters to wecmdb filters
#     '''
#     def _reverse_mapping(mappings):
#         '''reverse origin_field=>field mapping to field=>origin_field
#         '''
#         new_mappings = {}
#         for map_key, map_value in mappings.items():
#             new_mappings[map_value] = map_key
#         return new_mappings

#     def _transform_operator(operator):
#         '''transform talos operator to wecmdb operator
#         '''
#         mappings = {'ilike': 'contains', 'like': 'contains'}
#         return mappings.get(operator) or operator

#     def _transform_filter_field(filters, mappings):
#         '''transform talos filters to wecmdb filters
#         '''
#         new_filters = {}
#         for filter_key, filter_value in filters.items():
#             if filter_key in mappings:
#                 new_filters[mappings[filter_key]] = filter_value
#             else:
#                 new_filters[filter_key] = filter_value
#         return new_filters

#     def _transform_order_field(orders, mappings):
#         new_orders = []
#         for order in orders:
#             if order.startswith('+'):
#                 if order[1:] in mappings:
#                     new_orders.append('+' + mappings[order[1:]])
#                 else:
#                     new_orders.append(order)
#             elif order.startswith('-'):
#                 if order[1:] in mappings:
#                     new_orders.append('-' + mappings[order[1:]])
#                 else:
#                     new_orders.append(order)
#             else:
#                 if order in mappings:
#                     new_orders.append(mappings[order])
#                 else:
#                     new_orders.append(order)
#         return new_orders

#     if fields_mapping:
#         fields_mapping = _reverse_mapping(fields_mapping)
#     if filters and fields_mapping:
#         filters = _transform_filter_field(filters, fields_mapping)
#     if orders and fields_mapping:
#         orders = _transform_order_field(orders, fields_mapping)
#     query = {"dialect": {"showCiHistory": False}, "filters": [], "paging": False}
#     if offset and limit:
#         query['paging'] = True
#         query['pageable'] = {"pageSize": limit, "startIndex": offset}
#     if orders:
#         sortings = []
#         for order in orders:
#             if order.startswith('+'):
#                 sortings.append({"asc": True, "field": order[1:]})
#             elif order.startswith('-'):
#                 sortings.append({"asc": False, "field": order[1:]})
#             else:
#                 sortings.append({"asc": True, "field": order})
#         query['sortings'] = sortings
#     if filters:
#         new_filters = []
#         for filter_key, filter_value in filters.items():
#             if isinstance(filter_value, dict):
#                 for value_key, value_value in filter_value.items():
#                     new_filters.append({
#                         "name": filter_key,
#                         "operator": _transform_operator(value_key),
#                         "value": value_value
#                     })
#             else:
#                 new_filters.append({"name": filter_key, "operator": "eq", "value": filter_value})
#         query['filters'] = new_filters
#     return query


def transform_filter_to_entity_query(filters, fields_mapping=None):
    '''transform filter from talos filters to entity filters
    '''
    def _reverse_mapping(mappings):
        '''reverse origin_field=>field mapping to field=>origin_field
        '''
        new_mappings = {}
        for map_key, map_value in mappings.items():
            new_mappings[map_value] = map_key
        return new_mappings

    def _transform_operator(operator):
        '''transform talos operator to wecmdb operator
        '''
        mappings = {'ilike': 'contains', 'like': 'contains'}
        return mappings.get(operator) or operator

    def _transform_filter_field(filters, mappings):
        '''transform talos filters to wecmdb filters
        '''
        new_filters = {}
        for filter_key, filter_value in filters.items():
            if filter_key in mappings:
                new_filters[mappings[filter_key]] = filter_value
            else:
                new_filters[filter_key] = filter_value
        return new_filters

    if fields_mapping:
        fields_mapping = _reverse_mapping(fields_mapping)
    if filters and fields_mapping:
        filters = _transform_filter_field(filters, fields_mapping)
    query = {"additionalFilters": []}
    if filters:
        new_filters = []
        for filter_key, filter_value in filters.items():
            if isinstance(filter_value, dict):
                for value_key, value_value in filter_value.items():
                    new_filters.append({
                        "attrName": filter_key,
                        "op": _transform_operator(value_key),
                        "condition": value_value
                    })
            else:
                new_filters.append({"attrName": filter_key, "op": "eq", "condition": filter_value})
        query['additionalFilters'] = new_filters
    return query


def md5(text):
    hasher = hashlib.md5()
    hasher.update(text.encode())
    return hasher.hexdigest()


def aes_cbc_pkcs7_decrypt(encrypted, key, iv):
    key = utils.ensure_bytes(key)
    iv = utils.ensure_bytes(iv)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    data = cipher.decrypt(encrypted)
    # pkcs7 padding
    return data[0:-data[-1]]


def platform_encrypt(text, guid, seed):
    result = text
    encrypted_prefix = '{cipher_a}'
    if not text.startswith(encrypted_prefix):
        key = md5(guid + seed)[:16]
        key = utils.ensure_bytes(key)
        cipher = AES.new(key, AES.MODE_CBC, key)
        text = utils.ensure_bytes(text)
        # pkcs7 padding
        amount_to_pad = AES.block_size - (len(text) % AES.block_size)
        if amount_to_pad == 0:
            amount_to_pad = AES.block_size
        pad = chr(amount_to_pad).encode()
        encrypted_text = cipher.encrypt(text + pad * amount_to_pad)
        result = '{cipher_a}' + encrypted_text.hex()
    return result


def platform_decrypt(text, guid, seed):
    result = text
    encrypted_prefix = '{cipher_a}'
    if text.startswith(encrypted_prefix):
        encrypted_text = text[len(encrypted_prefix):]
        encrypted_text = bytes.fromhex(encrypted_text)
        key = md5(guid + seed)[:16]
        key = utils.ensure_bytes(key)
        cipher = AES.new(key, AES.MODE_CBC, key)
        origin_text = cipher.decrypt(encrypted_text)
        origin_text = origin_text[0:-origin_text[-1]]
        result = origin_text.decode()
    return result
