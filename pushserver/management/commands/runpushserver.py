import functools
import optparse
import re
import socket
import sys

from django.core.management import base as management_base
from django.core.management.commands import runserver

import hbpush
from hbpush import registry
from hbpush.pubsub import publisher, subscriber
from hbpush.store import memory, redis
import tornado
from tornado import httpserver, web, ioloop

DEFAULT_PORT = "8001"

default_store = {
    'redis': {
        'port': 6379,
        'host': 'localhost',
        'key_prefix': '',
        'database': 0,
    },
    'memory': {
        'min_messages': 0,
        'max_messages': 0,
        'message_timeout': 0,
    }
}

default_location = {
    'subscriber': {
        'polling': 'long',
        'create_on_get': False,
        'store': 'default',
    },
    'publisher': {
        'create_on_post': True,
        'store': 'default',
    }
}

defaults = {
    'port': DEFAULT_PORT,
    'address': '127.0.0.1',
    'store': {
        'type': 'memory',
    },
    'locations': (
        {
            'type': 'publisher',
            'prefix': '/publisher/',
        },
        {
            'type': 'subscriber',
            'prefix': '/subscriber/',
        }
    )
}

def make_store(store_dict):
    store_conf = default_store.get(store_dict['type'], {}).copy()
    store_conf.update(store_dict)

    store_type = store_conf.pop('type')
    if store_type == 'memory':
        cls = memory.MemoryStore
    elif store_type == 'redis':
        cls = redis.RedisStore
    else:
        raise management_base.CommandError('Invalid store type "%s".' % (store_type,))

    store = cls(**store_conf)
    return {
        'store': store,
        'registry': registry.Registry(store),
    }

def make_stores(stores_dict):
    if 'type' in stores_dict:
        stores_dict = {'default': stores_dict}
    return dict([(k, make_store(stores_dict[k])) for k in stores_dict])

def make_location(loc_dict, stores=None):
    if stores is None:
        stores = {}
    
    loc_conf = default_location.get(loc_dict['type'], {}).copy()
    loc_conf.update(loc_dict)

    loc_type = loc_conf.pop('type')
    if loc_type == 'publisher':
        cls = publisher.Publisher
    elif loc_type == 'subscriber':
        sub_type = loc_conf.pop('polling')
        if sub_type == 'long':
            cls = subscriber.LongPollingSubscriber
        elif sub_type == 'interval':
            cls = subscriber.Subscriber
        else:
            raise management_base.CommandError('Invalid polling "%s".' % (sub_type,))
    else:
        raise management_base.CommandError('Invalid location type "%s".' % (loc_type,))

    url = loc_conf.pop('url', loc_conf.pop('prefix', '')+'(.+)')
    store_id = loc_conf.pop('store')
    kwargs = {'registry': stores[store_id]['registry']}
    kwargs.update(loc_conf)
    return (url, cls, kwargs)

class Command(management_base.BaseCommand):
    option_list = management_base.BaseCommand.option_list + (
        optparse.make_option('--ipv6', '-6', action='store_true', dest='use_ipv6', default=False,
            help='Tells Django to use a IPv6 address.'),
    )
    help = "Starts a push server for development."
    args = '[optional port number, or ipaddr:port]'

    can_import_settings = True
    requires_model_validation = False

    def handle(self, addrport='', *args, **options):
        self.use_ipv6 = options.get('use_ipv6')
        if self.use_ipv6 and not socket.has_ipv6:
            raise management_base.CommandError('Your Python does not support IPv6.')
        if args:
            raise management_base.CommandError('Usage is runpushserver %s' % self.args)
        self._raw_ipv6 = False
        if not addrport:
            self.addr = ''
            self.port = DEFAULT_PORT
        else:
            m = re.match(runserver.naiveip_re, addrport)
            if m is None:
                raise management_base.CommandError('"%s" is not a valid port number or address:port pair.' % addrport)
            self.addr, _ipv4, _ipv6, _fqdn, self.port = m.groups()
            if not self.port.isdigit():
                raise management_base.CommandError("%r is not a valid port number." % self.port)
            if self.addr:
                if _ipv6:
                    self.addr = self.addr[1:-1]
                    self.use_ipv6 = True
                    self._raw_ipv6 = True
                elif self.use_ipv6 and not _fqdn:
                    raise management_base.CommandError('"%s" is not a valid IPv6 address.' % self.addr)
        if not self.addr:
            self.addr = self.use_ipv6 and '::1' or '127.0.0.1'
            self._raw_ipv6 = bool(self.use_ipv6)
        self.run(*args, **options)

    def run(self, *args, **options):
        from django.conf import settings
        
        quit_command = (sys.platform == 'win32') and 'CTRL-BREAK' or 'CONTROL-C'

        self.stdout.write((
            "Django version %(version)s, using settings %(settings)r\n"
            "Push server version %(push_version)s on Tornado version %(tornado_version)s\n"
            "Development push server is running at http://%(addr)s:%(port)s/\n"
            "Quit the server with %(quit_command)s.\n"
        ) % {
            "version": self.get_version(),
            "push_version": hbpush.__version__,
            "tornado_version": tornado.version,
            "settings": settings.SETTINGS_MODULE,
            "addr": self._raw_ipv6 and '[%s]' % self.addr or self.addr,
            "port": self.port,
            "quit_command": quit_command,
        })

        conf = defaults.copy()
        conf.update({
            'port': self.port,
            'address': self.addr,
        })
        conf.update(getattr(settings, 'PUSH_SERVER', {}))

        conf['store'] = make_stores(conf['store'])
        conf['locations'] = map(functools.partial(make_location, stores=conf['store']), conf['locations'])

        import logging
        logging.getLogger().setLevel('INFO')

        httpserver.HTTPServer(web.Application(conf['locations'])).listen(conf['port'], conf['address'])

        try:
            ioloop.IOLoop.instance().start()
        except KeyboardInterrupt:
            pass
