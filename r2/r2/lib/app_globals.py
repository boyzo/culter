# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
# 
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
# 
# The Original Code is Reddit.
# 
# The Original Developer is the Initial Developer.  The Initial Developer of the
# Original Code is CondeNet, Inc.
# 
# All portions of the code written by CondeNet are Copyright (c) 2006-2009
# CondeNet, Inc. All Rights Reserved.
################################################################################
from __future__ import with_statement
from pylons import config
import pytz, os, logging, sys, socket
from datetime import timedelta
from r2.lib.cache import LocalCache, Memcache, CacheChain
from r2.lib.db.stats import QueryStats
from r2.lib.translation import get_active_langs
from r2.lib.lock import make_lock_factory
from r2.lib.manager import db_manager

class Globals(object):

    int_props = ['page_cache_time',
                 'solr_cache_time',
                 'MIN_DOWN_LINK',
                 'MIN_UP_KARMA',
                 'MIN_DOWN_KARMA',
                 'MIN_RATE_LIMIT_KARMA',
                 'MIN_RATE_LIMIT_COMMENT_KARMA',
                 'WIKI_KARMA',
                 'HOT_PAGE_AGE',
                 'MODWINDOW',
                 'RATELIMIT',
                 'num_comments',
                 'max_comments',
                 'num_default_reddits',
                 'num_query_queue_workers',
                 'max_sr_images',
                 'num_serendipity',
                 'sr_dropdown_threshold',
                 ]

    float_props = ['min_promote_bid',
                   'max_promote_bid',
                   ]

    bool_props = ['debug', 'translator', 
                  'sqlprinting',
                  'template_debug',
                  'uncompressedJS',
                  'enable_doquery',
                  'use_query_cache',
                  'write_query_queue',
                  'show_awards',
                  'css_killswitch',
                  'db_create_tables',
                  'disallow_db_writes',
                  'allow_shutdown']

    tuple_props = ['memcaches',
                   'rec_cache',
                   'permacaches',
                   'rendercaches',
                   'admins',
                   'sponsors',
                   # TODO: temporary until we open it up to all users
                   'paid_sponsors',
                   'monitored_servers',
                   'automatic_reddits',
                   'agents',
                   'allowed_css_linked_domains']

    def __init__(self, global_conf, app_conf, paths, **extra):
        """
        Globals acts as a container for objects available throughout
        the life of the application.

        One instance of Globals is created by Pylons during
        application initialization and is available during requests
        via the 'g' variable.
        
        ``global_conf``
            The same variable used throughout ``config/middleware.py``
            namely, the variables from the ``[DEFAULT]`` section of the
            configuration file.
            
        ``app_conf``
            The same ``kw`` dictionary used throughout
            ``config/middleware.py`` namely, the variables from the
            section in the config file for your application.
            
        ``extra``
            The configuration returned from ``load_config`` in 
            ``config/middleware.py`` which may be of use in the setup of
            your global variables.
            
        """

        # slop over all variables to start with
        for k, v in  global_conf.iteritems():
            if not k.startswith("_") and not hasattr(self, k):
                if k in self.int_props:
                    v = int(v)
                elif k in self.float_props:
                    v = float(v)
                elif k in self.bool_props:
                    v = self.to_bool(v)
                elif k in self.tuple_props:
                    v = tuple(self.to_iter(v))
                setattr(self, k, v)

        self.paid_sponsors = set(x.lower() for x in self.paid_sponsors)

        # initialize caches
        mc = Memcache(self.memcaches, pickleProtocol = 1)
        self.memcache = mc
        self.cache = CacheChain((LocalCache(), mc))
        self.permacache = Memcache(self.permacaches, pickleProtocol = 1)
        self.rendercache = Memcache(self.rendercaches, pickleProtocol = 1)
        self.make_lock = make_lock_factory(mc)

        self.rec_cache = Memcache(self.rec_cache, pickleProtocol = 1)
        
        # set default time zone if one is not set
        tz = global_conf.get('timezone')
        dtz = global_conf.get('display_timezone', tz)

        self.tz = pytz.timezone(tz)
        self.display_tz = pytz.timezone(dtz)

        #load the database info
        self.dbm = self.load_db_params(global_conf)

        #make a query cache
        self.stats_collector = QueryStats()

        # set the modwindow
        self.MODWINDOW = timedelta(self.MODWINDOW)

        self.REDDIT_MAIN = bool(os.environ.get('REDDIT_MAIN'))

        # turn on for language support
        self.languages, self.lang_name = \
                        get_active_langs(default_lang= self.lang)

        all_languages = self.lang_name.keys()
        all_languages.sort()
        self.all_languages = all_languages

        # load the md5 hashes of files under static
        static_files = os.path.join(paths.get('static_files'), 'static')
        self.static_md5 = {}
        if os.path.exists(static_files):
            for f in os.listdir(static_files):
                if f.endswith('.md5'):
                    key = f.strip('.md5')
                    f = os.path.join(static_files, f)
                    with open(f, 'r') as handle:
                        md5 = handle.read().strip('\n')
                    self.static_md5[key] = md5


        #set up the logging directory
        log_path = self.log_path
        process_iden = global_conf.get('scgi_port', 'default')
        if log_path:
            if not os.path.exists(log_path):
                os.makedirs(log_path)
            for fname in os.listdir(log_path):
                if fname.startswith(process_iden):
                    full_name = os.path.join(log_path, fname)
                    os.remove(full_name)

        #setup the logger
        self.log = logging.getLogger('reddit')
        self.log.addHandler(logging.StreamHandler())
        if self.debug:
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.WARNING)

        # set log level for pycountry which is chatty
        logging.getLogger('pycountry.db').setLevel(logging.CRITICAL)

        if not self.media_domain:
            self.media_domain = self.domain
        if self.media_domain == self.domain:
            print "Warning: g.media_domain == g.domain. This may give untrusted content access to user cookies"

        #read in our CSS so that it can become a default for subreddit
        #stylesheets
        stylesheet_path = os.path.join(paths.get('static_files'),
                                       self.static_path.lstrip('/'),
                                       self.stylesheet)
        with open(stylesheet_path) as s:
            self.default_stylesheet = s.read()

        self.reddit_host = socket.gethostname()
        self.reddit_pid  = os.getpid()

        #the shutdown toggle
        self.shutdown = False

        #if we're going to use the query_queue, we need amqp
        if self.write_query_queue and not self.amqp_host:
            raise Exception("amqp_host must be defined to use the query queue")

    @staticmethod
    def to_bool(x):
        return (x.lower() == 'true') if x else None

    @staticmethod
    def to_iter(v, delim = ','):
        return (x.strip() for x in v.split(delim) if x)

    def load_db_params(self, gc):
        self.databases = tuple(self.to_iter(gc['databases']))
        self.db_params = {}
        if not self.databases:
            return

        dbm = db_manager.db_manager()
        db_param_names = ('name', 'db_host', 'db_user', 'db_pass',
                          'pool_size', 'max_overflow')
        for db_name in self.databases:
            conf_params = self.to_iter(gc[db_name + '_db'])
            params = dict(zip(db_param_names, conf_params))
            dbm.engines[db_name] = db_manager.get_engine(**params)
            self.db_params[db_name] = params

        dbm.type_db = dbm.engines[gc['type_db']]
        dbm.relation_type_db = dbm.engines[gc['rel_type_db']]

        prefix = 'db_table_'
        for k, v in gc.iteritems():
            if k.startswith(prefix):
                params = list(self.to_iter(v))
                name = k[len(prefix):]
                kind = params[0]
                if kind == 'thing':
                    dbm.add_thing(name, [dbm.engines[n] for n in params[1:]])
                elif kind == 'relation':
                    dbm.add_relation(name, params[1], params[2],
                                     [dbm.engines[n] for n in params[3:]])
        return dbm

    def __del__(self):
        """
        Put any cleanup code to be run when the application finally exits 
        here.
        """
        pass

