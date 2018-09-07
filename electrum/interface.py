#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import re
import socket
import ssl
import sys
import threading
import traceback
import aiorpcx
import asyncio
from aiorpcx import ClientSession, Notification

import requests

from .util import PrintError, aiosafe, bfh, AIOSafeSilentException

ca_path = requests.certs.where()

from . import util
from . import x509
from . import pem
from .version import ELECTRUM_VERSION, PROTOCOL_VERSION
from . import blockchain
from .blockchain import deserialize_header


class NotificationSession(ClientSession):

    def __init__(self, scripthash, header, *args, **kwargs):
        super(NotificationSession, self).__init__(*args, **kwargs)
        self.scripthash = scripthash
        self.header = header

    @aiosafe
    async def handle_request(self, request):
        if isinstance(request, Notification):
            if request.method == 'blockchain.scripthash.subscribe' and self.scripthash is not None:
                args = request.args
                await self.scripthash.put((args[0], args[1]))
            elif request.method == 'blockchain.headers.subscribe' and self.header is not None:
                deser = deserialize_header(bfh(request.args[0]['hex']), request.args[0]['height'])
                await self.header.put(deser)
            else:
                assert False, request.method



class GracefulDisconnect(AIOSafeSilentException): pass


class Interface(PrintError):

    def __init__(self, network, server, config_path, proxy):
        self.exception = None
        self.ready = asyncio.Future()
        self.server = server
        self.host, self.port, self.protocol = self.server.split(':')
        self.port = int(self.port)
        self.config_path = config_path
        self.cert_path = os.path.join(self.config_path, 'certs', self.host)
        self.fut = asyncio.get_event_loop().create_task(self.run())
        self.tip_header = None
        self.tip = 0
        self.blockchain = None
        self.network = network
        if proxy:
            username, pw = proxy.get('user'), proxy.get('password')
            if not username or not pw:
                auth = None
            else:
                auth = aiorpcx.socks.SOCKSUserAuth(username, pw)
            if proxy['mode'] == "socks4":
                self.proxy = aiorpcx.socks.SOCKSProxy((proxy['host'], int(proxy['port'])), aiorpcx.socks.SOCKS4a, auth)
            elif proxy['mode'] == "socks5":
                self.proxy = aiorpcx.socks.SOCKSProxy((proxy['host'], int(proxy['port'])), aiorpcx.socks.SOCKS5, auth)
            else:
                raise NotImplementedError # http proxy not available with aiorpcx
        else:
            self.proxy = None

    def diagnostic_name(self):
        return self.host

    async def is_server_ca_signed(self, sslc):
        try:
            await self.open_session(sslc, exit_early=True)
        except ssl.SSLError as e:
            assert e.reason == 'CERTIFICATE_VERIFY_FAILED'
            return False
        return True

    @aiosafe
    async def run(self):
        if self.protocol != 's':
            await self.open_session(None, exit_early=False)
            assert False

        ca_sslc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        exists = os.path.exists(self.cert_path)
        if exists:
            with open(self.cert_path, 'r') as f:
                contents = f.read()
            if contents != '': # if not CA signed
                try:
                    b = pem.dePem(contents, 'CERTIFICATE')
                except SyntaxError:
                    exists = False
                else:
                    x = x509.X509(b)
                    try:
                        x.check_date()
                    except x509.CertificateError as e:
                        self.print_error("certificate problem", e)
                        os.unlink(self.cert_path)
                        exists = False
        if not exists:
            try:
                ca_signed = await self.is_server_ca_signed(ca_sslc)
            except (ConnectionRefusedError, socket.gaierror) as e:
                self.print_error('disconnecting due to: {}'.format(e))
                self.exception = e
                return
            if ca_signed:
                with open(self.cert_path, 'w') as f:
                    # empty file means this is CA signed, not self-signed
                    f.write('')
            else:
                await self.save_certificate()
        siz = os.stat(self.cert_path).st_size
        if siz == 0: # if CA signed
            sslc = ca_sslc
        else:
            sslc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.cert_path)
            sslc.check_hostname = 0
        try:
            await self.open_session(sslc, exit_early=False)
        except (asyncio.CancelledError, ConnectionRefusedError, socket.gaierror, ssl.SSLError, TimeoutError) as e:
            if str(e):
                self.print_error('disconnecting due to: {}'.format(e))
            else:
                self.print_error('disconnecting due to exception of type: {}'.format(type(e)))
            self.exception = e
            return
        # should never get here (can only exit via exception)
        assert False

    def mark_ready(self):
        assert self.tip_header
        chain = blockchain.check_header(self.tip_header)
        if not chain:
            self.blockchain = blockchain.blockchains[0]
        else:
            self.blockchain = chain

        self.print_error("set blockchain with height", self.blockchain.height())

        if not self.ready.done():
            self.ready.set_result(1)

    async def save_certificate(self):
        if not os.path.exists(self.cert_path):
            # we may need to retry this a few times, in case the handshake hasn't completed
            for _ in range(10):
                dercert = await self.get_certificate()
                if dercert:
                    self.print_error("succeeded in getting cert")
                    with open(self.cert_path, 'w') as f:
                        cert = ssl.DER_cert_to_PEM_cert(dercert)
                        # workaround android bug
                        cert = re.sub("([^\n])-----END CERTIFICATE-----","\\1\n-----END CERTIFICATE-----",cert)
                        f.write(cert)
                        # even though close flushes we can't fsync when closed.
                        # and we must flush before fsyncing, cause flush flushes to OS buffer
                        # fsync writes to OS buffer to disk
                        f.flush()
                        os.fsync(f.fileno())
                    break
                await asyncio.sleep(1)
            else:
                assert False, "could not get certificate"

    async def get_certificate(self):
        sslc = ssl.SSLContext()
        try:
            async with aiorpcx.ClientSession(self.host, self.port, ssl=sslc, proxy=self.proxy) as session:
                return session.transport._ssl_protocol._sslpipe._sslobj.getpeercert(True)
        except ValueError:
            return None

    async def get_block_header(self, height, assert_mode):
        res = await asyncio.wait_for(self.session.send_request('blockchain.block.header', [height]), 1)
        return blockchain.deserialize_header(bytes.fromhex(res), height)

    async def request_chunk(self, idx, tip):
        return await self.network.request_chunk(idx, tip, self.session)

    async def open_session(self, sslc, exit_early):
        header_queue = asyncio.Queue()
        self.session = NotificationSession(None, header_queue, self.host, self.port, ssl=sslc, proxy=self.proxy)
        async with self.session as session:
            try:
                ver = await session.send_request('server.version', [ELECTRUM_VERSION, PROTOCOL_VERSION])
            except aiorpcx.jsonrpc.RPCError as e:
                raise GracefulDisconnect(e)  # probably 'unsupported protocol version'
            if exit_early:
                return
            self.print_error(ver, self.host)
            subscription_res = await session.send_request('blockchain.headers.subscribe')
            self.tip_header = blockchain.deserialize_header(bfh(subscription_res['hex']), subscription_res['height'])
            self.tip = subscription_res['height']
            self.mark_ready()
            copy_header_queue = asyncio.Queue()
            block_retriever = asyncio.get_event_loop().create_task(self.run_fetch_blocks(subscription_res, copy_header_queue))
            # make event such that we can cancel waiting
            # for the event without cancelling pm_task
            connection_lost_evt = asyncio.Event()
            session.pm_task.add_done_callback(lambda *args: connection_lost_evt.set())
            while not connection_lost_evt.is_set():
                try:
                    async with aiorpcx.curio.TimeoutAfter(300) as deadline:
                        async with aiorpcx.TaskGroup(wait=any) as tg:
                            qtask = await tg.spawn(header_queue.get())
                            await tg.spawn(connection_lost_evt.wait())
                except asyncio.CancelledError:
                    if not deadline.expired:
                        # if it wasn't because of the deadline, we are
                        # trying to shut down, and we shouldn't ping
                        raise
                    await asyncio.wait_for(session.send_request('server.ping'), 5)
                else:
                    try:
                        if qtask.done() and not qtask.exception():
                            new_header = qtask.result()
                            self.tip_header = new_header
                            self.tip = new_header['block_height']
                            await copy_header_queue.put(new_header)
                    except asyncio.CancelledError:
                        pass
            raise GracefulDisconnect("connection loop exited")

    def close(self):
        self.fut.cancel()

    @aiosafe
    async def run_fetch_blocks(self, sub_reply, replies):
        if self.tip < self.network.max_checkpoint():
            raise GracefulDisconnect('server tip below max checkpoint')

        async with self.network.bhi_lock:
            height = self.blockchain.height()+1
            await replies.put(blockchain.deserialize_header(bfh(sub_reply['hex']), sub_reply['height']))

        while True:
            self.network.notify('updated')
            item = await replies.get()
            async with self.network.bhi_lock:
                if self.blockchain.height() < item['block_height']-1:
                    _, height = await self.sync_until(height, None)
                if self.blockchain.height() >= height and self.blockchain.check_header(item):
                    # another interface amended the blockchain
                    self.print_error("SKIPPING HEADER", height)
                    continue
                if self.tip < height:
                    height = self.tip
                _, height = await self.step(height, item)
                self.tip = max(height, self.tip)

    async def sync_until(self, height, next_height=None):
        if next_height is None:
            next_height = self.tip
        last = None
        while last is None or height < next_height:
            if next_height > height + 10:
                could_connect, num_headers = await self.request_chunk(height, next_height)
                self.tip = max(height + num_headers, self.tip)
                if not could_connect:
                    if height <= self.network.max_checkpoint():
                        raise Exception('server chain conflicts with checkpoints or genesis')
                    last, height = await self.step(height)
                    self.tip = max(height, self.tip)
                    continue
                height = (height // 2016 * 2016) + num_headers
                if height > next_height:
                    assert False, (height, self.tip)
                last = 'catchup'
            else:
                last, height = await self.step(height)
                self.tip = max(height, self.tip)
        return last, height

    async def step(self, height, header=None):
        assert height != 0
        if header is None:
            header = await self.get_block_header(height, 'catchup')
        chain = self.blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
        if chain: return 'catchup', height
        can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)

        bad_header = None
        if not can_connect:
            self.print_error("can't connect", height)
            #backward
            bad = height
            bad_header = header
            height -= 1
            checkp = False
            if height <= self.network.max_checkpoint():
                height = self.network.max_checkpoint() + 1
                checkp = True

            header = await self.get_block_header(height, 'backward')
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
            if checkp:
                assert can_connect or chain, (can_connect, chain)
            while not chain and not can_connect:
                bad = height
                bad_header = header
                delta = self.tip - height
                next_height = self.tip - 2 * delta
                checkp = False
                if next_height <= self.network.max_checkpoint():
                    next_height = self.network.max_checkpoint() + 1
                    checkp = True
                height = next_height

                header = await self.get_block_header(height, 'backward')
                chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
                can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
                if checkp:
                    assert can_connect or chain, (can_connect, chain)
            self.print_error("exiting backward mode at", height)
        if can_connect:
            self.print_error("could connect", height)
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)

            if type(can_connect) is bool:
                # mock
                height += 1
                if height > self.tip:
                    assert False
                return 'catchup', height
            self.blockchain = can_connect
            height += 1
            self.blockchain.save_header(header)
            return 'catchup', height

        if not chain:
            raise Exception("not chain") # line 931 in 8e69174374aee87d73cd2f8005fbbe87c93eee9c's network.py

        # binary
        if type(chain) in [int, bool]:
            pass # mock
        else:
            self.blockchain = chain
        good = height
        height = (bad + good) // 2
        header = await self.get_block_header(height, 'binary')
        while True:
            self.print_error("binary step")
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            if chain:
                assert bad != height, (bad, height)
                good = height
                self.blockchain = self.blockchain if type(chain) in [bool, int] else chain
            else:
                bad = height
                assert good != height
                bad_header = header
            if bad != good + 1:
                height = (bad + good) // 2
                header = await self.get_block_header(height, 'binary')
                continue
            mock = bad_header and 'mock' in bad_header and bad_header['mock']['connect'](height)
            real = not mock and self.blockchain.can_connect(bad_header, check_height=False)
            if not real and not mock:
                raise Exception('unexpected bad header during binary' + str(bad_header)) # line 948 in 8e69174374aee87d73cd2f8005fbbe87c93eee9c's network.py
            branch = blockchain.blockchains.get(bad)
            if branch is not None:
                ismocking = False
                if type(branch) is dict:
                    ismocking = True
                # FIXME: it does not seem sufficient to check that the branch
                # contains the bad_header. what if self.blockchain doesn't?
                # the chains shouldn't be joined then. observe the incorrect
                # joining on regtest with a server that has a fork of height
                # one. the problem is observed only if forking is not during
                # electrum runtime
                if ismocking and branch['check'](bad_header) or not ismocking and branch.check_header(bad_header):
                    self.print_error('joining chain', bad)
                    height += 1
                    return 'join', height
                else:
                    if ismocking and branch['parent']['check'](header) or not ismocking and branch.parent().check_header(header):
                        self.print_error('reorg', bad, self.tip)
                        self.blockchain = branch.parent() if not ismocking else branch['parent']
                        height = bad
                        header = await self.get_block_header(height, 'binary')
                    else:
                        if ismocking:
                            height = bad + 1
                            self.print_error("TODO replace blockchain")
                            return 'conflict', height
                        self.print_error('forkpoint conflicts with existing fork', branch.path())
                        branch.write(b'', 0)
                        branch.save_header(bad_header)
                        self.blockchain = branch
                        height = bad + 1
                        return 'conflict', height
            else:
                bh = self.blockchain.height()
                if bh > good:
                    forkfun = self.blockchain.fork
                    if 'mock' in bad_header:
                        chain = bad_header['mock']['check'](bad_header)
                        forkfun = bad_header['mock']['fork'] if 'fork' in bad_header['mock'] else forkfun
                    else:
                        chain = self.blockchain.check_header(bad_header)
                    if not chain:
                        b = forkfun(bad_header)
                        assert bad not in blockchain.blockchains, (bad, list(blockchain.blockchains.keys()))
                        blockchain.blockchains[bad] = b
                        self.blockchain = b
                        height = b.forkpoint + 1
                        assert b.forkpoint == bad
                    return 'fork', height
                else:
                    assert bh == good
                    if bh < self.tip:
                        self.print_error("catching up from %d"% (bh + 1))
                        height = bh + 1
                    return 'no_fork', height

def check_cert(host, cert):
    try:
        b = pem.dePem(cert, 'CERTIFICATE')
        x = x509.X509(b)
    except:
        traceback.print_exc(file=sys.stdout)
        return

    try:
        x.check_date()
        expired = False
    except:
        expired = True

    m = "host: %s\n"%host
    m += "has_expired: %s\n"% expired
    util.print_msg(m)


# Used by tests
def _match_hostname(name, val):
    if val == name:
        return True

    return val.startswith('*.') and name.endswith(val[1:])


def test_certificates():
    from .simple_config import SimpleConfig
    config = SimpleConfig()
    mydir = os.path.join(config.path, "certs")
    certs = os.listdir(mydir)
    for c in certs:
        p = os.path.join(mydir,c)
        with open(p, encoding='utf-8') as f:
            cert = f.read()
        check_cert(c, cert)

if __name__ == "__main__":
    test_certificates()
