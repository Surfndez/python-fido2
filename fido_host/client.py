# Copyright (c) 2018 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import absolute_import

from .hid import CtapError
from .u2f import CTAP1, APDU, ApduError
from .fido2 import (CTAP2, PinProtocolV1, AttestedCredentialData,
                    AuthenticatorData, AttestationObject, AssertionResponse)
from .rpid import verify_rp_id, verify_app_id
from .utils import Timeout, sha256, hmac_sha256, websafe_decode, websafe_encode
from enum import IntEnum, unique
import json


class ClientData(bytes):
    def __init__(self, data):
        self.data = json.loads(data.decode())
        self.origin = self.data['origin']

    @property
    def b64(self):
        return websafe_encode(self)

    @property
    def hash(self):
        return sha256(self)

    @classmethod
    def build(cls, **kwargs):
        return cls(json.dumps(kwargs).encode())

    @classmethod
    def from_b64(cls, data):
        return cls(websafe_decode(data))

    def __repr__(self):
        return self.decode()

    def __str__(self):
        return self.decode()


class ClientError(Exception):

    @unique
    class ERR(IntEnum):
        OTHER_ERROR = 1
        BAD_REQUEST = 2
        CONFIGURATION_UNSUPPORTED = 3
        DEVICE_INELIGIBLE = 4
        TIMEOUT = 5

        def __call__(self):
            return ClientError(self)

    def __init__(self, code):
        self.code = ClientError.ERR(code)

    def __repr__(self):
        return 'U2F Client error: {0} - {0.name}'.format(self.code)


def _call_polling(poll_delay, timeout, func, *args, **kwargs):
    with Timeout(timeout or 30) as event:
        while not event.is_set():
            try:
                return func(*args, **kwargs)
            except ApduError as e:
                if e.code == APDU.USE_NOT_SATISFIED:
                    event.wait(poll_delay)
                else:
                    raise
        raise ClientError.ERR.TIMEOUT()


class U2fClient(object):
    def __init__(self, device, origin, verify=verify_app_id):
        self.poll_delay = 0.25
        self.ctap = CTAP1(device)
        self.origin = origin
        self._verify = verify_app_id

    def _verify_app_id(self, app_id):
        try:
            if self._verify(app_id, self.origin):
                return
        except Exception:
            pass  # Fall through to ClientError
        raise ClientError.ERR.BAD_REQUEST()

    def register(self, app_id, register_requests, registered_keys,
                 timeout=None):
        self._verify_app_id(app_id)

        version = self.ctap.get_version()
        dummy_param = b'\0'*32
        for key in registered_keys:
            if key['version'] != version:
                continue
            key_app_id = key.get('appId', app_id)
            app_param = sha256(key_app_id.encode())
            self._verify_app_id(key_app_id)
            key_handle = websafe_decode(key['keyHandle'])
            try:
                self.ctap.authenticate(dummy_param, app_param, key_handle, True)
                raise ClientError.ERR.DEVICE_INELIGIBLE()  # Bad response
            except ApduError as e:
                if e.code == APDU.USE_NOT_SATISFIED:
                    raise ClientError.ERR.DEVICE_INELIGIBLE()

        for request in register_requests:
            if request['version'] == version:
                challenge = request['challenge']
                break
        else:
            raise ClientError.ERR.DEVICE_INELIGIBLE()

        client_data = ClientData.build(
            typ='navigator.id.finishEnrollment',
            challenge=challenge,
            origin=self.origin
        )
        app_param = sha256(app_id.encode())

        reg_data = _call_polling(self.poll_delay, timeout, self.ctap.register,
                                 client_data.hash, app_param)
        return {
            'registrationData': reg_data.b64,
            'clientData': client_data.b64
        }

    def sign(self, app_id, challenge, registered_keys, timeout=None):
        client_data = ClientData.build(
            typ='navigator.id.getAssertion',
            challenge=challenge,
            origin=self.origin
        )

        version = self.ctap.get_version()
        for key in registered_keys:
            if key['version'] == version:
                key_app_id = key.get('appId', app_id)
                self._verify_app_id(key_app_id)
                key_handle = websafe_decode(key['keyHandle'])
                app_param = sha256(key_app_id.encode())
                try:
                    signature_data = _call_polling(
                        self.poll_delay, timeout, self.ctap.authenticate,
                        client_data.hash, app_param, key_handle)
                    break
                except ApduError:
                    pass  # Ignore and try next key
        else:
            raise ClientError.ERR.DEVICE_INELIGIBLE()

        return {
            'clientData': client_data.b64,
            'signatureData': signature_data.b64,
            'keyHandle': key['keyHandle']
        }


@unique
class CRED_ALGO(IntEnum):
    ES256 = -7
    RS256 = -257


class Fido2Client(object):
    def __init__(self, device, origin, verify=verify_rp_id):
        self.ctap1_poll_delay = 0.25
        self.origin = origin
        self._verify = verify
        try:
            self.ctap = CTAP2(device)
            self.pin_protocol = PinProtocolV1(self.ctap)
            self._do_make_credential = self._ctap2_make_credential
            self._do_get_assertion = self._ctap2_get_assertion
        except ValueError:
            self.ctap = CTAP1(device)
            self._do_make_credential = self._ctap1_make_credential
            self._do_get_assertion = self._ctap1_get_assertion

    def _verify_rp_id(self, rp_id):
        try:
            if self._verify(rp_id, self.origin):
                return
        except Exception:
            pass  # Fall through to ClientError
        raise ClientError.ERR.BAD_REQUEST()

    def make_credential(self, rp, user, challenge, algos=[CRED_ALGO.ES256],
                        exclude_list=None, extensions=None, rk=False, uv=False,
                        pin=None, timeout=None):
        self._verify_rp_id(rp['id'])

        client_data = ClientData.build(
            type='webauthn.create',
            clientExtensions={},
            challenge=challenge,
            origin=self.origin
        )

        attestation = self._do_make_credential(
            client_data, rp, user, algos, exclude_list, extensions, rk, uv, pin,
            timeout)
        return attestation, client_data

    def _ctap2_make_credential(self, client_data, rp, user, algos, exclude_list,
                               extensions, rk, uv, pin, timeout):
        key_params = [{'type': 'public-key', 'alg': alg} for alg in algos]

        info = self.ctap.get_info()
        pin_auth = None
        pin_protocol = None
        if pin:
            pin_protocol = self.pin_protocol.VERSION
            if pin_protocol not in info.pin_protocols:
                raise ValueError('Device does not support PIN protocol 1!')
            pin_token = self.pin_protocol.get_pin_token(pin)
            pin_auth = hmac_sha256(pin_token, client_data.hash)[:16]
        elif info.options.get('clientPin'):
            raise ValueError('PIN required!')

        if not (rk or uv):
            options = None
        else:
            options = {}
            if rk:
                options['rk'] = True
            if uv:
                options['uv'] = True

        return self.ctap.make_credential(client_data.hash, rp, user,
                                         key_params, exclude_list,
                                         extensions, options, pin_auth,
                                         pin_protocol, timeout)

    def _ctap1_make_credential(self, client_data, rp, user, algos, exclude_list,
                               extensions, rk, uv, pin, timeout):
        if rk or uv:
            raise CtapError(CtapError.ERR.UNSUPPORTED_OPTION)

        app_param = sha256(rp['id'].encode())

        dummy_param = b'\0'*32
        for cred in exclude_list or []:
            key_handle = cred['id']
            try:
                self.ctap.authenticate(dummy_param, app_param, key_handle, True)
                raise CtapError(CtapError.ERR.CREDENTIAL_EXCLUDED)  # Invalid
            except ApduError as e:
                if e.code == APDU.USE_NOT_SATISFIED:
                    raise CtapError(CtapError.ERR.CREDENTIAL_EXCLUDED)

        reg_resp = _call_polling(self.ctap1_poll_delay, timeout,
                                 self.ctap.register, client_data.hash,
                                 app_param)

        return AttestationObject.create(
            'fido-u2f',
            AuthenticatorData.create(
                app_param,
                0x41,
                0,
                AttestedCredentialData.create(
                    b'\0'*16,  # aaguid
                    reg_resp.key_handle,
                    {  # EC256 public key
                        1: 2,
                        3: -7,
                        -1: 1,
                        -2: reg_resp.public_key[1:1+32],
                        -3: reg_resp.public_key[33:33+32]
                    }
                )
            ),
            {  # att_statement
                'x5c': [reg_resp.certificate],
                'sig': reg_resp.signature
            }
        )

    def get_assertion(self, rp_id, challenge, allow_list=None, extensions=None,
                      rk=False, uv=False, pin=None, timeout=None):
        self._verify_rp_id(rp_id)

        client_data = ClientData.build(
            type='webauthn.get',
            clientExtensions={},
            challenge=challenge,
            origin=self.origin
        )

        assertions = self._do_get_assertion(
            client_data, rp_id, allow_list, extensions, rk, uv, pin, timeout)
        return assertions, client_data

    def _ctap2_get_assertion(self, client_data, rp_id, allow_list, extensions,
                             rk, uv, pin, timeout):
        info = self.ctap.get_info()
        pin_auth = None
        pin_protocol = None
        if pin:
            pin_protocol = self.pin_protocol.VERSION
            if pin_protocol not in info.pin_protocols:
                raise ValueError('Device does not support PIN protocol 1!')
            pin_token = self.pin_protocol.get_pin_token(pin)
            pin_auth = hmac_sha256(pin_token, client_data.hash)[:16]
        elif info.options.get('clientPin'):
            raise ValueError('PIN required!')

        if not (rk or uv):
            options = None
        else:
            options = {}
            if rk:
                options['rk'] = True
            if uv:
                options['uv'] = True

        assertions = [self.ctap.get_assertion(rp_id, client_data.hash,
                                              allow_list, extensions, options,
                                              pin_auth, pin_protocol, timeout)]
        for _ in range((assertions[0].number_of_credentials or 1) - 1):
            assertions.append(self.ctap.get_next_assertion())
        return assertions

    def _ctap1_get_assertion(self, client_data, rp_id, allow_list, extensions,
                             rk, uv, pin, timeout):
        if rk or uv or not allow_list:
            raise CtapError(CtapError.ERR.UNSUPPORTED_OPTION)

        app_param = sha256(rp_id.encode())
        client_param = client_data.hash
        for cred in allow_list:
            try:
                auth_resp = _call_polling(self.ctap1_poll_delay, timeout,
                                          self.ctap.authenticate, client_param,
                                          app_param, cred['id'])
                return [AssertionResponse.create(
                    cred,
                    AuthenticatorData.create(
                        app_param,
                        auth_resp.user_presence & 0x01,
                        auth_resp.counter
                    ),
                    auth_resp.signature
                )]
            except ApduError:
                pass  # Ignore this handle
        raise CtapError(CtapError.ERR.NO_CREDENTIALS)