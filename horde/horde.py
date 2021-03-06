#!/usr/bin/env python
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

from . import argparse
from . import BitTornado.BT1.track as bttrack
from . import BitTornado.BT1.makemetafile as makemetafile
from . import murder_client as murder_client


opts = {}
log = logging.getLogger('horde')
log.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                              '%Y-%m-%d %H:%M:%S')
ch.setFormatter(formatter)
log.addHandler(ch)

horde_root = os.path.dirname(os.path.realpath(__file__))
bittornado_tgz = os.path.join(horde_root, 'bittornado.tar.gz')
murderclient_py = os.path.join(horde_root, 'murder_client.py')
argparse_py = os.path.join(horde_root, 'argparse.py')  # For < 2.7 compat
horde_py = os.path.join(horde_root, 'horde.py')


def retries(max_tries, delay=1, backoff=2, exceptions=(Exception,), hook=None):
    """Function decorator implementing retrying logic.

    delay: Sleep this many seconds * backoff * try number after failure
    backoff: Multiply delay by this factor after each failure
    exceptions: A tuple of exception classes; default (Exception,)
    hook: A function with the signature myhook(tries_remaining, exception);
          default None

    The decorator will call the function up to max_tries times if it raises
    an exception."""
    def dec(func):
        def f2(*args, **kwargs):
            mydelay = delay
            tries = list(range(max_tries))
            tries.reverse()
            for tries_remaining in tries:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if tries_remaining > 0:
                        if hook is not None:
                            hook(func.__name__, tries_remaining, e, mydelay)
                        time.sleep(mydelay)
                        mydelay = mydelay * backoff
                    else:
                        raise
                else:
                    break
        return f2
    return dec


def run(local_file, remote_file, hosts):
    start = time.time()
    log.info("Spawning tracker...")
    t = threading.Thread(target=track)
    t.daemon = True
    t.start()
    local_host = (local_ip(), opts['port'])
    log.info("Creating torrent (host %s:%s)..." % local_host)
    torrent_file = mktorrent(local_file, '%s:%s' % local_host)
    log.info("Seeding %s" % torrent_file)
    s = threading.Thread(target=seed, args=(torrent_file, local_file,))
    s.daemon = True
    s.start()
    log.info("Transferring")
    if not os.path.isfile(bittornado_tgz):
        cwd = os.getcwd()
        os.chdir(horde_root)
        args = ['tar', 'czf', 'bittornado.tar.gz', 'BitTornado']
        log.info("Executing: " + " ".join(args))
        subprocess.call(args)
        os.chdir(cwd)
    threads = []
    for host in hosts:
        if remote_file == 'sr-mount':  # Grab sr path on host
            try:
                sr_uuid = get_sr_uuid(host)
            except Exception:
                print(' FAIL: Unable to determine SR UUID for host %s' % host, file=sys.stderr)
                continue  # Continue transferring to other hosts
            remote_path = sr_uuid + '/' + os.path.basename(local_file)
        else:
            remote_path = remote_file
        td = threading.Thread(target=transfer, args=(
            host,
            torrent_file,
            remote_path,
            opts['retry']))
        td.start()
        threads.append(td)
    [td.join() for td in threads]
    os.unlink(torrent_file)
    try:
        os.unlink(opts['data_file'])
    except OSError:
        pass
    # cleanup
    for host in hosts:
        ssh(host, 'rm %s' % (
            opts['remote_path'] + '/' + os.path.basename(torrent_file)))
    log.info("Finished, took %.2f seconds." % (time.time() - start))


def transfer(host, local_file, remote_target, retry=0):
    rp = opts['remote_path']
    file_name = os.path.basename(local_file)
    remote_file = '%s/%s' % (rp, file_name)
    if ssh(host, 'test -d %s/BitTornado' % rp) != 0:
        ssh(host, "mkdir -p %s" % rp)
        scp(host, bittornado_tgz, '%s/bittornado.tar.gz' % rp)
        ssh(host, "cd %s; tar zxvf bittornado.tar.gz > /dev/null" % rp)
        scp(host, murderclient_py, '%s/murder_client.py' % rp)
        scp(host, argparse_py, '%s/argparse.py' % rp)
        scp(host, horde_py, '%s/horde.py' % rp)
    log.info("Copying %s to %s:%s" % (local_file, host, remote_file))
    scp(host, local_file, remote_file)
    command = 'python %s/murder_client.py peer %s %s' % (
        rp,
        remote_file,
        remote_target)
    log.info("running \"%s\" on %s", command, host)
    result = ssh(host, command)
    if result == 0:
        cmd = 'python %s/horde.py %s %s --seed True' % (
            rp,
            remote_file,
            remote_target)
        s = threading.Thread(target=ssh, args=(host, cmd,))
        s.daemon = True
        s.start()
    else:
        log.info("%s FAILED with code %s" % (host, result))
        while retry != 0:
            retry = retry - 1
            log.info("retrying on %s" % host)
            transfer(host, local_file, remote_target, 0)
    return host


@retries(3)
def get_sr_uuid(host):
    command = "df -h |grep sr-mount|awk -F ' ' '{print $5}'|tr -d '\n'"
    output = subprocess.Popen([
        'ssh', '-o UserKnownHostsFile=/dev/null',
        '-o ConnectTimeout=300', '-o ServerAliveInterval=60',
        '-o TCPKeepAlive=yes', '-o LogLevel=quiet',
        '-o StrictHostKeyChecking=no', host, command],
        stdout=subprocess.PIPE).communicate()[0]
    if not str(output):
        raise Exception('Unable to get host sr-uuid')
    return str(output)


@retries(3)
def ssh(host, command):
    if not os.path.exists(opts['log_dir']):
        os.makedirs(opts['log_dir'])

    with open("%s%s%s-ssh.log" % (opts['log_dir'], os.path.sep, host),
              'a') as log:
        result = subprocess.call([
            'ssh', '-o UserKnownHostsFile=/dev/null',
            '-o ConnectTimeout=300',
            '-o ServerAliveInterval=60',
            '-o TCPKeepAlive=yes',
            '-o LogLevel=quiet',
            '-o StrictHostKeyChecking=no',
            host, command], stdout=log,
            stderr=log)
    return result


@retries(3)
def scp(host, local_file, remote_file):
    return subprocess.call([
        'scp', '-o UserKnownHostsFile=/dev/null',
        '-o StrictHostKeyChecking=no',
        local_file, '%s:%s' % (host, remote_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def mktorrent(file_name, tracker):
    torrent_file = tempfile.mkstemp('.torrent')
    makemetafile.make_meta_file(file_name, "http://%s/announce" % tracker,
                                {'target': torrent_file[1],
                                    'piece_size_pow2': 0})
    return torrent_file[1]


def track():
    bttrack.track(["--dfile", opts['data_file'], "--port", opts['port']])


def seed(torrent, local_file):
    murder_client.run([
        "--responsefile", torrent,
        "--saveas", local_file])


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("10.1.0.0", 0))
    return s.getsockname()[0]


def hordemain():
    if not os.path.exists(opts['hosts']) and opts['hostlist'] is False:
        sys.exit('ERROR: hosts file "%s" does not exist' % opts['hosts'])

    if opts['hosts']:
        hosts = [line.strip() for line in open(opts['hosts'], 'r')]
        # filter out comments and empty lines
        hosts = [
            host for host in hosts if not re.match("^#", host)
            and host is not '']
    else:
        hosts = opts['hostlist'].split(',')
    # handles duplicates
    hosts = list(set(hosts))
    log.info("Running with options: %s" % opts)
    log.info("Running for hosts: %s" % hosts)
    run(opts['local-file'], opts['remote-file'], hosts)


def run_with_opts(local_file, remote_file, hosts='', retry=0, port=8998,
                  remote_path='/tmp/horde', data_file='./data',
                  log_dir='/tmp/horde', hostlist=False):
    """Can include horde into existing python easier."""
    global opts
    opts['local-file'] = local_file
    opts['remote-file'] = remote_file
    opts['hosts'] = hosts
    opts['retry'] = retry
    opts['port'] = port
    opts['remote_path'] = remote_path
    opts['data_file'] = data_file
    opts['log_dir'] = log_dir
    opts['hostlist'] = hostlist
    hordemain()


def entry_point():
    global opts
    parser = argparse.ArgumentParser()
    parser.add_argument('local-file',
                        help='Local file to upload')

    parser.add_argument('remote-file',
                        help="Remote file destination")

    parser.add_argument('hosts',
                        help="File containing list of hosts",
                        default='',
                        nargs='?')

    parser.add_argument('--retry',
                        default=0,
                        type=int,
                        help="Number of times to retry in case of failure. " +
                        "Use -1 to make it retry forever (not recommended)")

    parser.add_argument('--port',
                        default=8998,
                        help="Port number to run the tracker on")

    parser.add_argument('--remote-path',
                        default='/tmp/horde',
                        help="Temporary path to store uploads")

    parser.add_argument('--data-file',
                        default='./data',
                        help="Temporary file to store for bittornado.")

    parser.add_argument('--log-dir',
                        default='/tmp/horde',
                        help="Path to the directory for murder logs")

    parser.add_argument('--hostlist',
                        default=False,
                        help="Comma separated list of hots")

    parser.add_argument('--seed',
                        default=False,
                        help="Seed local file from torrent")

    opts = vars(parser.parse_args())

    if opts['seed']:
        seed(opts['local-file'], opts['remote-file'])
    else:
        hordemain()

if __name__ == '__main__':
    entry_point()
