# (c) 2014, Chris Church <chris@ninemoreminutes.com>
#
# This file is part of Ansible.
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import base64
import os
import re
import shlex
import traceback

from six.moves.urllib import parse

try:
    from winrm import Response
    from winrm.exceptions import WinRMTransportError
    from winrm.protocol import Protocol
except ImportError:
    raise AnsibleError("winrm is not installed")

HAVE_KERBEROS = False
try:
    import kerberos
    HAVE_KERBEROS = True
except ImportError:
    pass

from ansible import constants as C
from ansible.errors import AnsibleError, AnsibleConnectionFailure, AnsibleFileNotFound
from ansible.plugins.connections import ConnectionBase
from ansible.plugins import shell_loader
from ansible.utils.path import makedirs_safe
from ansible.utils.unicode import to_bytes


class Connection(ConnectionBase):
    '''WinRM connections over HTTP/HTTPS.'''

    transport_schemes = {
        'http': [('kerberos', 'http'), ('plaintext', 'http'), ('plaintext', 'https')],
        'https': [('kerberos', 'https'), ('plaintext', 'https')],
        }

    def __init__(self,  *args, **kwargs):

        self.has_pipelining   = False
        self.default_suffixes = ['.ps1', '']
        self.protocol         = None
        self.shell_id         = None
        self.delegate         = None

        self._shell           = shell_loader.get('powershell')

        # TODO: Add runas support
        self.become_methods_supported=[]

        super(Connection, self).__init__(*args, **kwargs)

    @property
    def transport(self):
        ''' used to identify this connection object from other classes '''
        return 'winrm'

    def _winrm_connect(self):
        '''
        Establish a WinRM connection over HTTP/HTTPS.
        '''
        port = self._connection_info.port or 5986
        self._display.vvv("ESTABLISH WINRM CONNECTION FOR USER: %s on PORT %s TO %s" % \
            (self._connection_info.remote_user, port, self._connection_info.remote_addr), host=self._connection_info.remote_addr)
        netloc = '%s:%d' % (self._connection_info.remote_addr, port)
        exc = None
        for transport, scheme in self.transport_schemes['http' if port == 5985 else 'https']:
            if transport == 'kerberos' and (not HAVE_KERBEROS or not '@' in self._connection_info.remote_user):
                continue

            if transport == 'kerberos':
                realm = self._connection_info.remote_user.split('@', 1)[1].strip() or None
            else:
                realm = None

            endpoint = parse.urlunsplit((scheme, netloc, '/wsman', '', ''))

            self._display.vvvvv('WINRM CONNECT: transport=%s endpoint=%s' % (transport, endpoint), host=self._connection_info.remote_addr)
            protocol = Protocol(
                endpoint,
                transport=transport,
                username=self._connection_info.remote_user,
                password=self._connection_info.password,
                realm=realm
            )

            try:
                protocol.send_message('')
                return protocol
            except WinRMTransportError as exc:
                err_msg = str(exc)
                if re.search(r'Operation\s+?timed\s+?out', err_msg, re.I):
                    raise AnsibleError("the connection attempt timed out")
                m = re.search(r'Code\s+?(\d{3})', err_msg)
                if m:
                    code = int(m.groups()[0])
                    if code == 401:
                        raise AnsibleError("the username/password specified for this server was incorrect")
                    elif code == 411:
                        return protocol
                self._display.vvvvv('WINRM CONNECTION ERROR: %s' % err_msg, host=self._connection_info.remote_addr)
                continue
        if exc:
            raise AnsibleError(str(exc))

    def _winrm_exec(self, command, args=(), from_exec=False):
        if from_exec:
            self._display.vvvvv("WINRM EXEC %r %r" % (command, args), host=self._connection_info.remote_addr)
        else:
            self._display.vvvvvv("WINRM EXEC %r %r" % (command, args), host=self._connection_info.remote_addr)
        if not self.protocol:
            self.protocol = self._winrm_connect()
        if not self.shell_id:
            self.shell_id = self.protocol.open_shell()
        command_id = None
        try:
            command_id = self.protocol.run_command(self.shell_id, command, args)
            response = Response(self.protocol.get_command_output(self.shell_id, command_id))
            if from_exec:
                self._display.vvvvv('WINRM RESULT %r' % response, host=self._connection_info.remote_addr)
            else:
                self._display.vvvvvv('WINRM RESULT %r' % response, host=self._connection_info.remote_addr)
            self._display.vvvvvv('WINRM STDOUT %s' % response.std_out, host=self._connection_info.remote_addr)
            self._display.vvvvvv('WINRM STDERR %s' % response.std_err, host=self._connection_info.remote_addr)
            return response
        finally:
            if command_id:
                self.protocol.cleanup_command(self.shell_id, command_id)

    def _connect(self):
        if not self.protocol:
            self.protocol = self._winrm_connect()
        return self

    def exec_command(self, cmd, tmp_path, in_data=None, sudoable=True):
        super(Connection, self).exec_command(cmd, tmp_path, in_data=in_data, sudoable=sudoable)

        cmd = to_bytes(cmd)
        cmd_parts = shlex.split(cmd, posix=False)
        if '-EncodedCommand' in cmd_parts:
            encoded_cmd = cmd_parts[cmd_parts.index('-EncodedCommand') + 1]
            decoded_cmd = base64.b64decode(encoded_cmd)
            self._display.vvv("EXEC %s" % decoded_cmd, host=self._connection_info.remote_addr)
        else:
            self._display.vvv("EXEC %s" % cmd, host=self._connection_info.remote_addr)
        # For script/raw support.
        if cmd_parts and cmd_parts[0].lower().endswith('.ps1'):
            script = self._shell._build_file_cmd(cmd_parts, quote_args=False)
            cmd_parts = self._shell._encode_script(script, as_list=True)
        try:
            result = self._winrm_exec(cmd_parts[0], cmd_parts[1:], from_exec=True)
        except Exception as e:
            traceback.print_exc()
            raise AnsibleError("failed to exec cmd %s" % cmd)
        result.std_out = to_bytes(result.std_out)
        result.std_err = to_bytes(result.std_err)
        return (result.status_code, '', result.std_out, result.std_err)

    def put_file(self, in_path, out_path):
        super(Connection, self).put_file(in_path, out_path)

        self._display.vvv("PUT %s TO %s" % (in_path, out_path), host=self._connection_info.remote_addr)
        if not os.path.exists(in_path):
            raise AnsibleFileNotFound("file or module does not exist: %s" % in_path)
        with open(in_path) as in_file:
            in_size = os.path.getsize(in_path)
            script_template = '''
                $s = [System.IO.File]::OpenWrite("%s");
                [void]$s.Seek(%d, [System.IO.SeekOrigin]::Begin);
                $b = [System.Convert]::FromBase64String("%s");
                [void]$s.Write($b, 0, $b.length);
                [void]$s.SetLength(%d);
                [void]$s.Close();
            '''
            # Determine max size of data we can pass per command.
            script = script_template % (self._shell._escape(out_path), in_size, '', in_size)
            cmd = self._shell._encode_script(script)
            # Encode script with no data, subtract its length from 8190 (max
            # windows command length), divide by 2.67 (UTF16LE base64 command
            # encoding), then by 1.35 again (data base64 encoding).
            buffer_size = int(((8190 - len(cmd)) / 2.67) / 1.35)
            for offset in xrange(0, in_size, buffer_size):
                try:
                    out_data = in_file.read(buffer_size)
                    if offset == 0:
                        if out_data.lower().startswith('#!powershell') and not out_path.lower().endswith('.ps1'):
                            out_path = out_path + '.ps1'
                    b64_data = base64.b64encode(out_data)
                    script = script_template % (self._shell._escape(out_path), offset, b64_data, in_size)
                    self._display.vvvvv("WINRM PUT %s to %s (offset=%d size=%d)" % (in_path, out_path, offset, len(out_data)), host=self._connection_info.remote_addr)
                    cmd_parts = self._shell._encode_script(script, as_list=True)
                    result = self._winrm_exec(cmd_parts[0], cmd_parts[1:])
                    if result.status_code != 0:
                        raise IOError(result.std_err.encode('utf-8'))
                except Exception:
                    traceback.print_exc()
                    raise AnsibleError("failed to transfer file to %s" % out_path)

    def fetch_file(self, in_path, out_path):
        super(Connection, self).fetch_file(in_path, out_path)

        out_path = out_path.replace('\\', '/')
        self._display.vvv("FETCH %s TO %s" % (in_path, out_path), host=self._connection_info.remote_addr)
        buffer_size = 2**19 # 0.5MB chunks
        makedirs_safe(os.path.dirname(out_path))
        out_file = None
        try:
            offset = 0
            while True:
                try:
                    script = '''
                        If (Test-Path -PathType Leaf "%(path)s")
                        {
                            $stream = [System.IO.File]::OpenRead("%(path)s");
                            $stream.Seek(%(offset)d, [System.IO.SeekOrigin]::Begin) | Out-Null;
                            $buffer = New-Object Byte[] %(buffer_size)d;
                            $bytesRead = $stream.Read($buffer, 0, %(buffer_size)d);
                            $bytes = $buffer[0..($bytesRead-1)];
                            [System.Convert]::ToBase64String($bytes);
                            $stream.Close() | Out-Null;
                        }
                        ElseIf (Test-Path -PathType Container "%(path)s")
                        {
                            Write-Host "[DIR]";
                        }
                        Else
                        {
                            Write-Error "%(path)s does not exist";
                            Exit 1;
                        }
                    ''' % dict(buffer_size=buffer_size, path=self._shell._escape(in_path), offset=offset)
                    self._display.vvvvv("WINRM FETCH %s to %s (offset=%d)" % (in_path, out_path, offset), host=self._connection_info.remote_addr)
                    cmd_parts = self._shell._encode_script(script, as_list=True)
                    result = self._winrm_exec(cmd_parts[0], cmd_parts[1:])
                    if result.status_code != 0:
                        raise IOError(result.std_err.encode('utf-8'))
                    if result.std_out.strip() == '[DIR]':
                        data = None
                    else:
                        data = base64.b64decode(result.std_out.strip())
                    if data is None:
                        makedirs_safe(out_path)
                        break
                    else:
                        if not out_file:
                            # If out_path is a directory and we're expecting a file, bail out now.
                            if os.path.isdir(out_path):
                                break
                            out_file = open(out_path, 'wb')
                        out_file.write(data)
                        if len(data) < buffer_size:
                            break
                        offset += len(data)
                except Exception:
                    traceback.print_exc()
                    raise AnsibleError("failed to transfer file to %s" % out_path)
        finally:
            if out_file:
                out_file.close()

    def close(self):
        if self.protocol and self.shell_id:
            self.protocol.close_shell(self.shell_id)
            self.shell_id = None
