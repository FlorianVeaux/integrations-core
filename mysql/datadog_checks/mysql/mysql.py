# (C) Datadog, Inc. 2013-present
# (C) Patrick Galbraith <patg@patg.net> 2013
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
from __future__ import division

import re
import traceback
from collections import defaultdict, namedtuple
from contextlib import closing, contextmanager

import pymysql
from six import PY3, iteritems, itervalues

from datadog_checks.base import AgentCheck, is_affirmative
from datadog_checks.base.utils.db import QueryManager

from .collection_utils import collect_all_scalars, collect_scalar, collect_string, collect_type
from .const import (
    BINLOG_VARS,
    BUILDS,
    COUNT,
    GALERA_VARS,
    GAUGE,
    INNODB_VARS,
    MONOTONIC,
    OPTIONAL_INNODB_VARS,
    OPTIONAL_STATUS_VARS,
    OPTIONAL_STATUS_VARS_5_6_6,
    PERFORMANCE_VARS,
    PROC_NAME,
    RATE,
    REPLICA_VARS,
    SCHEMA_VARS,
    STATUS_VARS,
    SYNTHETIC_VARS,
    VARIABLES_VARS,
)
from .queries import (
    SQL_95TH_PERCENTILE,
    SQL_AVG_QUERY_RUN_TIME,
    SQL_INNODB_ENGINES,
    SQL_PROCESS_LIST,
    SQL_QUERY_SCHEMA_SIZE,
    SQL_WORKER_THREADS,
)

try:
    import psutil

    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


if PY3:
    long = int


MySQLMetadata = namedtuple('MySQLMetadata', ['version', 'flavor', 'build'])


class MySql(AgentCheck):
    SERVICE_CHECK_NAME = 'mysql.can_connect'
    SLAVE_SERVICE_CHECK_NAME = 'mysql.replication.slave_running'
    DEFAULT_MAX_CUSTOM_QUERIES = 20

    def __init__(self, name, init_config, instances):
        super(MySql, self).__init__(name, init_config, instances)
        self.qcache_stats = {}
        self.metadata = None

        self._tags = list(self.instance.get('tags', []))

        # Create a new connection on every check run
        self._conn = None

        self._query_manager = QueryManager(self, self.execute_query_raw, queries=[], tags=self._tags)
        self.check_initializations.append(self._query_manager.compile_queries)

    def execute_query_raw(self, query):
        with closing(self._conn.cursor(pymysql.cursors.SSCursor)) as cursor:
            cursor.execute(query)
            for row in cursor.fetchall_unbuffered():
                yield row

    def _get_metadata(self, db):
        with closing(db.cursor()) as cursor:
            cursor.execute('SELECT VERSION()')
            result = cursor.fetchone()

            # Version might include a build, a flavor, or both
            # e.g. 4.1.26-log, 4.1.26-MariaDB, 10.0.1-MariaDB-mariadb1precise-log
            # See http://dev.mysql.com/doc/refman/4.1/en/information-functions.html#function_version
            # https://mariadb.com/kb/en/library/version/
            # and https://mariadb.com/kb/en/library/server-system-variables/#version
            parts = result[0].split('-')
            version, flavor, build = [parts[0], '', '']

            for data in parts:
                if data == "MariaDB":
                    flavor = "MariaDB"
                if data != "MariaDB" and flavor == '':
                    flavor = "MySQL"
                if data in BUILDS:
                    build = data
            if build == '':
                build = 'unspecified'

            return MySQLMetadata(version, flavor, build)

    def _send_metadata(self):
        self.set_metadata('version', self.metadata.version + '+' + self.metadata.build)
        self.set_metadata('flavor', self.metadata.flavor)

    @classmethod
    def get_library_versions(cls):
        return {'pymysql': pymysql.__version__}

    def check(self, instance):
        (
            host,
            port,
            user,
            password,
            mysql_sock,
            defaults_file,
            tags,
            options,
            queries,
            ssl,
            connect_timeout,
            max_custom_queries,
        ) = self._get_config(instance)

        self._set_qcache_stats()

        if not (host and user) and not defaults_file:
            raise Exception("Mysql host and user are needed.")

        with self._connect(host, port, mysql_sock, user, password, defaults_file, ssl, connect_timeout, tags) as db:
            try:
                self._conn = db

                # metadata collection
                self.metadata = self._get_metadata(db)
                self._send_metadata()

                # Metric collection
                self._collect_metrics(db, tags, options, queries, max_custom_queries)
                self._collect_system_metrics(host, db, tags)

                # keeping track of these:
                self._put_qcache_stats()

                # Custom queries
                self._query_manager.execute()

            except Exception as e:
                self.log.exception("error!")
                raise e
            finally:
                self._conn = None

    def _get_config(self, instance):
        self.host = instance.get('server', '')
        self.port = int(instance.get('port', 0))
        self.mysql_sock = instance.get('sock', '')
        self.defaults_file = instance.get('defaults_file', '')
        user = instance.get('user', '')
        password = str(instance.get('pass', ''))
        tags = instance.get('tags', [])
        options = instance.get('options', {}) or {}  # options could be None if empty in the YAML
        queries = instance.get('queries', [])
        ssl = instance.get('ssl', {})
        connect_timeout = instance.get('connect_timeout', 10)
        max_custom_queries = instance.get('max_custom_queries', self.DEFAULT_MAX_CUSTOM_QUERIES)

        if queries or 'max_custom_queries' in instance:
            self.warning(
                'The options `queries` and `max_custom_queries` are deprecated and will be '
                'removed in a future release. Use the `custom_queries` option instead.'
            )

        return (
            self.host,
            self.port,
            user,
            password,
            self.mysql_sock,
            self.defaults_file,
            tags,
            options,
            queries,
            ssl,
            connect_timeout,
            max_custom_queries,
        )

    def _set_qcache_stats(self):
        host_key = self._get_host_key()
        qcache_st = self.qcache_stats.get(host_key, (None, None, None))

        self._qcache_hits = qcache_st[0]
        self._qcache_inserts = qcache_st[1]
        self._qcache_not_cached = qcache_st[2]

    def _put_qcache_stats(self):
        host_key = self._get_host_key()
        self.qcache_stats[host_key] = (self._qcache_hits, self._qcache_inserts, self._qcache_not_cached)

    def _get_host_key(self):
        if self.defaults_file:
            return self.defaults_file

        hostkey = self.host
        if self.mysql_sock:
            hostkey = "{0}:{1}".format(hostkey, self.mysql_sock)
        elif self.port:
            hostkey = "{0}:{1}".format(hostkey, self.port)

        return hostkey

    @contextmanager
    def _connect(self, host, port, mysql_sock, user, password, defaults_file, ssl, connect_timeout, tags):
        self.service_check_tags = [
            'server:%s' % (mysql_sock if mysql_sock != '' else host),
            'port:%s' % ('unix_socket' if port == 0 else port),
        ]

        if tags is not None:
            self.service_check_tags.extend(tags)

        db = None
        try:
            ssl = dict(ssl) if ssl else None

            if defaults_file != '':
                db = pymysql.connect(read_default_file=defaults_file, ssl=ssl, connect_timeout=connect_timeout)
            elif mysql_sock != '':
                self.service_check_tags = ['server:{0}'.format(mysql_sock), 'port:unix_socket'] + tags
                db = pymysql.connect(
                    unix_socket=mysql_sock, user=user, passwd=password, connect_timeout=connect_timeout
                )
            elif port:
                db = pymysql.connect(
                    host=host, port=port, user=user, passwd=password, ssl=ssl, connect_timeout=connect_timeout
                )
            else:
                db = pymysql.connect(host=host, user=user, passwd=password, ssl=ssl, connect_timeout=connect_timeout)
            self.log.debug("Connected to MySQL")
            self.service_check_tags = list(set(self.service_check_tags))
            self.service_check(self.SERVICE_CHECK_NAME, AgentCheck.OK, tags=self.service_check_tags)
            yield db
        except Exception:
            self.service_check(self.SERVICE_CHECK_NAME, AgentCheck.CRITICAL, tags=self.service_check_tags)
            raise
        finally:
            if db:
                db.close()

    def _collect_metrics(self, db, tags, options, queries, max_custom_queries):

        # Get aggregate of all VARS we want to collect
        metrics = STATUS_VARS

        # collect results from db
        results = self._get_stats_from_status(db)
        results.update(self._get_stats_from_variables(db))

        if not is_affirmative(options.get('disable_innodb_metrics', False)) and self._is_innodb_engine_enabled(db):
            results.update(self._get_stats_from_innodb_status(db))

            innodb_keys = [
                'Innodb_page_size',
                'Innodb_buffer_pool_pages_data',
                'Innodb_buffer_pool_pages_dirty',
                'Innodb_buffer_pool_pages_total',
                'Innodb_buffer_pool_pages_free',
            ]

            for inno_k in innodb_keys:
                results[inno_k] = collect_scalar(inno_k, results)

            try:
                innodb_page_size = results['Innodb_page_size']
                innodb_buffer_pool_pages_used = (
                    results['Innodb_buffer_pool_pages_total'] - results['Innodb_buffer_pool_pages_free']
                )

                if 'Innodb_buffer_pool_bytes_data' not in results:
                    results['Innodb_buffer_pool_bytes_data'] = (
                        results['Innodb_buffer_pool_pages_data'] * innodb_page_size
                    )

                if 'Innodb_buffer_pool_bytes_dirty' not in results:
                    results['Innodb_buffer_pool_bytes_dirty'] = (
                        results['Innodb_buffer_pool_pages_dirty'] * innodb_page_size
                    )

                if 'Innodb_buffer_pool_bytes_free' not in results:
                    results['Innodb_buffer_pool_bytes_free'] = (
                        results['Innodb_buffer_pool_pages_free'] * innodb_page_size
                    )

                if 'Innodb_buffer_pool_bytes_total' not in results:
                    results['Innodb_buffer_pool_bytes_total'] = (
                        results['Innodb_buffer_pool_pages_total'] * innodb_page_size
                    )

                if 'Innodb_buffer_pool_pages_utilization' not in results:
                    results['Innodb_buffer_pool_pages_utilization'] = (
                        innodb_buffer_pool_pages_used / results['Innodb_buffer_pool_pages_total']
                    )

                if 'Innodb_buffer_pool_bytes_used' not in results:
                    results['Innodb_buffer_pool_bytes_used'] = innodb_buffer_pool_pages_used * innodb_page_size
            except (KeyError, TypeError) as e:
                self.log.error("Not all InnoDB buffer pool metrics are available, unable to compute: %s", e)

            if is_affirmative(options.get('extra_innodb_metrics', False)):
                self.log.debug("Collecting Extra Innodb Metrics")
                metrics.update(OPTIONAL_INNODB_VARS)

        # Binary log statistics
        if self._get_variable_enabled(results, 'log_bin'):
            results['Binlog_space_usage_bytes'] = self._get_binary_log_stats(db)

        # Compute key cache utilization metric
        key_blocks_unused = collect_scalar('Key_blocks_unused', results)
        key_cache_block_size = collect_scalar('key_cache_block_size', results)
        key_buffer_size = collect_scalar('key_buffer_size', results)
        results['Key_buffer_size'] = key_buffer_size

        try:
            # can be null if the unit is missing in the user config (4 instead of 4G for eg.)
            if key_buffer_size != 0:
                key_cache_utilization = 1 - ((key_blocks_unused * key_cache_block_size) / key_buffer_size)
                results['Key_cache_utilization'] = key_cache_utilization

            results['Key_buffer_bytes_used'] = collect_scalar('Key_blocks_used', results) * key_cache_block_size
            results['Key_buffer_bytes_unflushed'] = (
                collect_scalar('Key_blocks_not_flushed', results) * key_cache_block_size
            )
        except TypeError as e:
            self.log.error("Not all Key metrics are available, unable to compute: %s", e)

        metrics.update(VARIABLES_VARS)
        metrics.update(INNODB_VARS)
        metrics.update(BINLOG_VARS)

        if is_affirmative(options.get('extra_status_metrics', False)):
            self.log.debug("Collecting Extra Status Metrics")
            metrics.update(OPTIONAL_STATUS_VARS)

            if self._version_compatible(db, (5, 6, 6)):
                metrics.update(OPTIONAL_STATUS_VARS_5_6_6)

        if is_affirmative(options.get('galera_cluster', False)):
            # already in result-set after 'SHOW STATUS' just add vars to collect
            self.log.debug("Collecting Galera Metrics.")
            metrics.update(GALERA_VARS)

        performance_schema_enabled = self._get_variable_enabled(results, 'performance_schema')
        above_560 = self._version_compatible(db, (5, 6, 0))
        if is_affirmative(options.get('extra_performance_metrics', False)) and above_560 and performance_schema_enabled:
            # report avg query response time per schema to Datadog
            results['perf_digest_95th_percentile_avg_us'] = self._get_query_exec_time_95th_us(db)
            results['query_run_time_avg'] = self._query_exec_time_per_schema(db)
            metrics.update(PERFORMANCE_VARS)

        if is_affirmative(options.get('schema_size_metrics', False)):
            # report avg query response time per schema to Datadog
            results['information_schema_size'] = self._query_size_per_schema(db)
            metrics.update(SCHEMA_VARS)

        if is_affirmative(options.get('replication', False)):
            # Get replica stats
            is_mariadb = self.metadata.flavor == "MariaDB"
            replication_channel = options.get('replication_channel')
            if replication_channel:
                self.service_check_tags.append("channel:{0}".format(replication_channel))
                tags.append("channel:{0}".format(replication_channel))
            results.update(self._get_replica_stats(db, is_mariadb, replication_channel))
            nonblocking = is_affirmative(options.get('replication_non_blocking_status', False))
            results.update(self._get_slave_status(db, above_560, nonblocking))
            metrics.update(REPLICA_VARS)

            # get slave running form global status page
            slave_running_status = AgentCheck.UNKNOWN
            slave_running = collect_string('Slave_running', results)
            binlog_running = results.get('Binlog_enabled', False)
            # slaves will only be collected iff user has PROCESS privileges.
            slaves = collect_scalar('Slaves_connected', results)
            slave_io_running = collect_type('Slave_IO_Running', results, dict)
            slave_sql_running = collect_type('Slave_SQL_Running', results, dict)
            if slave_io_running:
                slave_io_running = any(v.lower().strip() == 'yes' for v in itervalues(slave_io_running))
            if slave_sql_running:
                slave_sql_running = any(v.lower().strip() == 'yes' for v in itervalues(slave_sql_running))

            # MySQL 5.7.x might not have 'Slave_running'. See: https://bugs.mysql.com/bug.php?id=78544
            # look at replica vars collected at the top of if-block
            if self._version_compatible(db, (5, 7, 0)):
                if not (slave_io_running is None and slave_sql_running is None):
                    if slave_io_running and slave_sql_running:
                        slave_running_status = AgentCheck.OK
                    elif not slave_io_running and not slave_sql_running:
                        slave_running_status = AgentCheck.CRITICAL
                    else:
                        # not everything is running smoothly
                        slave_running_status = AgentCheck.WARNING
            elif slave_running.lower().strip() == 'off':
                if not (slave_io_running is None and slave_sql_running is None):
                    if not slave_io_running and not slave_sql_running:
                        slave_running_status = AgentCheck.CRITICAL

            # if we don't yet have a status - inspect
            if slave_running_status == AgentCheck.UNKNOWN:
                if self._is_master(slaves, results):  # master
                    if slaves > 0 and binlog_running:
                        slave_running_status = AgentCheck.OK
                    else:
                        slave_running_status = AgentCheck.WARNING
                elif slave_running:  # slave (or standalone)
                    if slave_running.lower().strip() == 'on':
                        slave_running_status = AgentCheck.OK
                    else:
                        slave_running_status = AgentCheck.CRITICAL

            # deprecated in favor of service_check("mysql.replication.slave_running")
            self.gauge(self.SLAVE_SERVICE_CHECK_NAME, 1 if slave_running_status == AgentCheck.OK else 0, tags=tags)
            self.service_check(self.SLAVE_SERVICE_CHECK_NAME, slave_running_status, tags=self.service_check_tags)

        # "synthetic" metrics
        metrics.update(SYNTHETIC_VARS)
        self._compute_synthetic_results(results)

        # remove uncomputed metrics
        for k in SYNTHETIC_VARS:
            if k not in results:
                metrics.pop(k, None)

        # add duped metrics - reporting some as both rate and gauge
        dupes = [
            ('Table_locks_waited', 'Table_locks_waited_rate'),
            ('Table_locks_immediate', 'Table_locks_immediate_rate'),
        ]
        for src, dst in dupes:
            if src in results:
                results[dst] = results[src]

        self._submit_metrics(metrics, results, tags)

        # Collect custom query metrics
        # Max of 20 queries allowed
        if isinstance(queries, list):
            for check in queries[:max_custom_queries]:
                total_tags = tags + check.get('tags', [])
                self._collect_dict(
                    check['type'], {check['field']: check['metric']}, check['query'], db, tags=total_tags
                )

            if len(queries) > max_custom_queries:
                self.warning("Maximum number (%s) of custom queries reached.  Skipping the rest.", max_custom_queries)

    def _is_master(self, slaves, results):
        # master uuid only collected in slaves
        master_host = collect_string('Master_Host', results)
        if slaves > 0 or not master_host:
            return True

        return False

    def _submit_metrics(self, variables, db_results, tags):
        for variable, metric in iteritems(variables):
            metric_name, metric_type = metric
            for tag, value in collect_all_scalars(variable, db_results):
                metric_tags = list(tags)
                if tag:
                    metric_tags.append(tag)
                if value is not None:
                    if metric_type == RATE:
                        self.rate(metric_name, value, tags=metric_tags)
                    elif metric_type == GAUGE:
                        self.gauge(metric_name, value, tags=metric_tags)
                    elif metric_type == COUNT:
                        self.count(metric_name, value, tags=metric_tags)
                    elif metric_type == MONOTONIC:
                        self.monotonic_count(metric_name, value, tags=metric_tags)

    def _version_compatible(self, db, compat_version):
        # some patch version numbers contain letters (e.g. 5.0.51a)
        # so let's be careful when we compute the version number

        try:
            mysql_version = self.metadata.version.split('.')
        except Exception as e:
            self.warning("Cannot compute mysql version, assuming it's older.: %s", e)
            return False
        self.log.debug("MySQL version %s", mysql_version)

        patchlevel = int(re.match(r"([0-9]+)", mysql_version[2]).group(1))
        version = (int(mysql_version[0]), int(mysql_version[1]), patchlevel)

        return version >= compat_version

    def _collect_dict(self, metric_type, field_metric_map, query, db, tags):
        """
        Query status and get a dictionary back.
        Extract each field out of the dictionary
        and stuff it in the corresponding metric.

        query: show status...
        field_metric_map: {"Seconds_behind_master": "mysqlSecondsBehindMaster"}
        """
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute(query)
                result = cursor.fetchone()
                if result is not None:
                    for field, metric in list(iteritems(field_metric_map)):
                        # Find the column name in the cursor description to identify the column index
                        # http://www.python.org/dev/peps/pep-0249/
                        # cursor.description is a tuple of (column_name, ..., ...)
                        try:
                            col_idx = [d[0].lower() for d in cursor.description].index(field.lower())
                            self.log.debug("Collecting metric: %s", metric)
                            if result[col_idx] is not None:
                                self.log.debug("Collecting done, value %s", result[col_idx])
                                if metric_type == GAUGE:
                                    self.gauge(metric, float(result[col_idx]), tags=tags)
                                elif metric_type == RATE:
                                    self.rate(metric, float(result[col_idx]), tags=tags)
                                else:
                                    self.gauge(metric, float(result[col_idx]), tags=tags)
                            else:
                                self.log.debug("Received value is None for index %d", col_idx)
                        except ValueError:
                            self.log.exception("Cannot find %s in the columns %s", field, cursor.description)
        except Exception:
            self.warning("Error while running %s\n%s", query, traceback.format_exc())
            self.log.exception("Error while running %s", query)

    def _collect_system_metrics(self, host, db, tags):
        pid = None
        # The server needs to run locally, accessed by TCP or socket
        if host in ["localhost", "127.0.0.1", "0.0.0.0"] or db.port == long(0):
            pid = self._get_server_pid(db)

        if pid:
            self.log.debug("System metrics for mysql w/ pid: %s", pid)
            # At last, get mysql cpu data out of psutil or procfs

            try:
                ucpu, scpu = None, None
                if PSUTIL_AVAILABLE:
                    proc = psutil.Process(pid)

                    ucpu = proc.cpu_times()[0]
                    scpu = proc.cpu_times()[1]

                if ucpu and scpu:
                    self.rate("mysql.performance.user_time", ucpu, tags=tags)
                    # should really be system_time
                    self.rate("mysql.performance.kernel_time", scpu, tags=tags)
                    self.rate("mysql.performance.cpu_time", ucpu + scpu, tags=tags)

            except Exception:
                self.warning("Error while reading mysql (pid: %s) procfs data\n%s", pid, traceback.format_exc())

    def _get_pid_file_variable(self, db):
        """
        Get the `pid_file` variable
        """
        pid_file = None
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute("SHOW VARIABLES LIKE 'pid_file'")
                pid_file = cursor.fetchone()[1]
        except Exception:
            self.warning("Error while fetching pid_file variable of MySQL.")

        return pid_file

    def _get_server_pid(self, db):
        pid = None

        # Try to get pid from pid file, it can fail for permission reason
        pid_file = self._get_pid_file_variable(db)
        if pid_file is not None:
            self.log.debug("pid file: %s", str(pid_file))
            try:
                with open(pid_file, 'rb') as f:
                    pid = int(f.readline())
            except IOError:
                self.log.debug("Cannot read mysql pid file %s", pid_file)

        # If pid has not been found, read it from ps
        if pid is None and PSUTIL_AVAILABLE:
            for proc in psutil.process_iter():
                try:
                    if proc.name() == PROC_NAME:
                        pid = proc.pid
                except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess):
                    continue
                except Exception:
                    self.log.exception("Error while fetching mysql pid from psutil")

        return pid

    @classmethod
    def _get_stats_from_status(cls, db):
        with closing(db.cursor()) as cursor:
            cursor.execute("SHOW /*!50002 GLOBAL */ STATUS;")
            results = dict(cursor.fetchall())

            return results

    @classmethod
    def _get_stats_from_variables(cls, db):
        with closing(db.cursor()) as cursor:
            cursor.execute("SHOW GLOBAL VARIABLES;")
            results = dict(cursor.fetchall())

            return results

    def _get_binary_log_stats(self, db):
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute("SHOW BINARY LOGS;")
                cursor_results = cursor.fetchall()
                master_logs = {result[0]: result[1] for result in cursor_results}

                binary_log_space = 0
                for value in itervalues(master_logs):
                    binary_log_space += value

                return binary_log_space
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("Privileges error accessing the BINARY LOGS (must grant REPLICATION CLIENT): %s", e)
            return None

    def _is_innodb_engine_enabled(self, db):
        # Whether InnoDB engine is available or not can be found out either
        # from the output of SHOW ENGINES or from information_schema.ENGINES
        # table. Later is choosen because that involves no string parsing.
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute(SQL_INNODB_ENGINES)
                return cursor.rowcount > 0

        except (pymysql.err.InternalError, pymysql.err.OperationalError, pymysql.err.NotSupportedError) as e:
            self.warning("Possibly innodb stats unavailable - error querying engines table: %s", e)
            return False

    def _get_replica_stats(self, db, is_mariadb, replication_channel):
        replica_results = defaultdict(dict)
        try:
            with closing(db.cursor(pymysql.cursors.DictCursor)) as cursor:
                if is_mariadb and replication_channel:
                    cursor.execute("SET @@default_master_connection = '{0}';".format(replication_channel))
                    cursor.execute("SHOW SLAVE STATUS;")
                elif replication_channel:
                    cursor.execute("SHOW SLAVE STATUS FOR CHANNEL '{0}';".format(replication_channel))
                else:
                    cursor.execute("SHOW SLAVE STATUS;")

                for slave_result in cursor.fetchall():
                    # MySQL <5.7 does not have Channel_Name.
                    # For MySQL >=5.7 'Channel_Name' is set to an empty string by default
                    channel = replication_channel or slave_result.get('Channel_Name') or 'default'
                    for key, value in iteritems(slave_result):
                        if value is not None:
                            replica_results[key]['channel:{0}'.format(channel)] = value
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            errno, msg = e.args
            if errno == 1617 and msg == "There is no master connection '{0}'".format(replication_channel):
                # MariaDB complains when you try to get slave status with a
                # connection name on the master, without connection name it
                # responds an empty string as expected.
                # Mysql behaves the same with or without connection name.
                pass
            else:
                self.warning("Privileges error getting replication status (must grant REPLICATION CLIENT): %s", e)

        try:
            with closing(db.cursor(pymysql.cursors.DictCursor)) as cursor:
                cursor.execute("SHOW MASTER STATUS;")
                binlog_results = cursor.fetchone()
                if binlog_results:
                    replica_results.update({'Binlog_enabled': True})
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("Privileges error getting binlog information (must grant REPLICATION CLIENT): %s", e)

        return replica_results

    def _get_slave_status(self, db, above_560, nonblocking):
        """
        Retrieve the slaves' statuses using:
        1. The `performance_schema.threads` table. Non-blocking, requires version > 5.6.0
        2. The `information_schema.processlist` table. Blocking
        """
        try:
            with closing(db.cursor()) as cursor:
                if above_560 and nonblocking:
                    # Query `performance_schema.threads` instead of `
                    # information_schema.processlist` to avoid mutex impact on performance.
                    cursor.execute(SQL_WORKER_THREADS)
                else:
                    cursor.execute(SQL_PROCESS_LIST)
                slave_results = cursor.fetchall()
                slaves = 0
                for _ in slave_results:
                    slaves += 1

                return {'Slaves_connected': slaves}

        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("Privileges error accessing the process tables (must grant PROCESS): %s", e)
            return {}

    @classmethod
    def _are_values_numeric(cls, array):
        return all(v.isdigit() for v in array)

    def _get_stats_from_innodb_status(self, db):
        # There are a number of important InnoDB metrics that are reported in
        # InnoDB status but are not otherwise present as part of the STATUS
        # variables in MySQL. Majority of these metrics are reported though
        # as a part of STATUS variables in Percona Server and MariaDB.
        # Requires querying user to have PROCESS privileges.
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute("SHOW /*!50000 ENGINE*/ INNODB STATUS")
        except (pymysql.err.InternalError, pymysql.err.OperationalError, pymysql.err.NotSupportedError) as e:
            self.warning(
                "Privilege error or engine unavailable accessing the INNODB status tables (must grant PROCESS): %s", e,
            )
            return {}
        except (UnicodeDecodeError, UnicodeEncodeError) as e:
            self.log.warning(
                "Unicode error while getting INNODB status "
                "(typically harmless, but if this warning is frequent metric collection could be impacted): %s",
                str(e),
            )
            return {}

        if cursor.rowcount < 1:
            # No data from SHOW ENGINE STATUS, even though the engine is enabled.
            # EG: This could be an Aurora Read Instance
            self.warning(
                """'SHOW ENGINE INNODB STATUS' returned no data.
                If you are running an Aurora Read Instance, \
                this is expected and you should disable the innodb metrics collection"""
            )
            return {}

        innodb_status = cursor.fetchone()
        innodb_status_text = innodb_status[2]

        results = defaultdict(int)

        # Here we now parse InnoDB STATUS one line at a time
        # This is heavily inspired by the Percona monitoring plugins work
        txn_seen = False
        prev_line = ''
        # Only return aggregated buffer pool metrics
        buffer_id = -1
        for line in innodb_status_text.splitlines():
            line = line.strip()
            row = re.split(" +", line)
            row = [item.strip(',') for item in row]
            row = [item.strip(';') for item in row]
            row = [item.strip('[') for item in row]
            row = [item.strip(']') for item in row]

            if line.startswith('---BUFFER POOL'):
                buffer_id = long(row[2])

            # SEMAPHORES
            if line.find('Mutex spin waits') == 0:
                # Mutex spin waits 79626940, rounds 157459864, OS waits 698719
                # Mutex spin waits 0, rounds 247280272495, OS waits 316513438
                results['Innodb_mutex_spin_waits'] = long(row[3])
                results['Innodb_mutex_spin_rounds'] = long(row[5])
                results['Innodb_mutex_os_waits'] = long(row[8])
            elif line.find('RW-shared spins') == 0 and line.find(';') > 0:
                # RW-shared spins 3859028, OS waits 2100750; RW-excl spins
                # 4641946, OS waits 1530310
                results['Innodb_s_lock_spin_waits'] = long(row[2])
                results['Innodb_x_lock_spin_waits'] = long(row[8])
                results['Innodb_s_lock_os_waits'] = long(row[5])
                results['Innodb_x_lock_os_waits'] = long(row[11])
            elif line.find('RW-shared spins') == 0 and line.find('; RW-excl spins') == -1:
                # Post 5.5.17 SHOW ENGINE INNODB STATUS syntax
                # RW-shared spins 604733, rounds 8107431, OS waits 241268
                results['Innodb_s_lock_spin_waits'] = long(row[2])
                results['Innodb_s_lock_spin_rounds'] = long(row[4])
                results['Innodb_s_lock_os_waits'] = long(row[7])
            elif line.find('RW-excl spins') == 0:
                # Post 5.5.17 SHOW ENGINE INNODB STATUS syntax
                # RW-excl spins 604733, rounds 8107431, OS waits 241268
                results['Innodb_x_lock_spin_waits'] = long(row[2])
                results['Innodb_x_lock_spin_rounds'] = long(row[4])
                results['Innodb_x_lock_os_waits'] = long(row[7])
            elif line.find('seconds the semaphore:') > 0:
                # --Thread 907205 has waited at handler/ha_innodb.cc line 7156 for 1.00 seconds the semaphore:
                results['Innodb_semaphore_waits'] += 1
                results['Innodb_semaphore_wait_time'] += long(float(row[9])) * 1000

            # TRANSACTIONS
            elif line.find('Trx id counter') == 0:
                # The beginning of the TRANSACTIONS section: start counting
                # transactions
                # Trx id counter 0 1170664159
                # Trx id counter 861B144C
                txn_seen = True
            elif line.find('History list length') == 0:
                # History list length 132
                results['Innodb_history_list_length'] = long(row[3])
            elif txn_seen and line.find('---TRANSACTION') == 0:
                # ---TRANSACTION 0, not started, process no 13510, OS thread id 1170446656
                results['Innodb_current_transactions'] += 1
                if line.find('ACTIVE') > 0:
                    results['Innodb_active_transactions'] += 1
            elif txn_seen and line.find('------- TRX HAS BEEN') == 0:
                # ------- TRX HAS BEEN WAITING 32 SEC FOR THIS LOCK TO BE GRANTED:
                results['Innodb_row_lock_time'] += long(row[5]) * 1000
            elif line.find('read views open inside InnoDB') > 0:
                # 1 read views open inside InnoDB
                results['Innodb_read_views'] = long(row[0])
            elif line.find('mysql tables in use') == 0:
                # mysql tables in use 2, locked 2
                results['Innodb_tables_in_use'] += long(row[4])
                results['Innodb_locked_tables'] += long(row[6])
            elif txn_seen and line.find('lock struct(s)') > 0:
                # 23 lock struct(s), heap size 3024, undo log entries 27
                # LOCK WAIT 12 lock struct(s), heap size 3024, undo log entries 5
                # LOCK WAIT 2 lock struct(s), heap size 368
                if line.find('LOCK WAIT') == 0:
                    results['Innodb_lock_structs'] += long(row[2])
                    results['Innodb_locked_transactions'] += 1
                elif line.find('ROLLING BACK') == 0:
                    # ROLLING BACK 127539 lock struct(s), heap size 15201832,
                    # 4411492 row lock(s), undo log entries 1042488
                    results['Innodb_lock_structs'] += long(row[2])
                else:
                    results['Innodb_lock_structs'] += long(row[0])

            # FILE I/O
            elif line.find(' OS file reads, ') > 0:
                # 8782182 OS file reads, 15635445 OS file writes, 947800 OS
                # fsyncs
                results['Innodb_os_file_reads'] = long(row[0])
                results['Innodb_os_file_writes'] = long(row[4])
                results['Innodb_os_file_fsyncs'] = long(row[8])
            elif line.find('Pending normal aio reads:') == 0:
                try:
                    if len(row) == 8:
                        # (len(row) == 8)  Pending normal aio reads: 0, aio writes: 0,
                        results['Innodb_pending_normal_aio_reads'] = long(row[4])
                        results['Innodb_pending_normal_aio_writes'] = long(row[7])
                    elif len(row) == 14:
                        # (len(row) == 14) Pending normal aio reads: 0 [0, 0] , aio writes: 0 [0, 0] ,
                        results['Innodb_pending_normal_aio_reads'] = long(row[4])
                        results['Innodb_pending_normal_aio_writes'] = long(row[10])
                    elif len(row) == 16:
                        # (len(row) == 16) Pending normal aio reads: [0, 0, 0, 0] , aio writes: [0, 0, 0, 0] ,
                        if self._are_values_numeric(row[4:8]) and self._are_values_numeric(row[11:15]):
                            results['Innodb_pending_normal_aio_reads'] = (
                                long(row[4]) + long(row[5]) + long(row[6]) + long(row[7])
                            )
                            results['Innodb_pending_normal_aio_writes'] = (
                                long(row[11]) + long(row[12]) + long(row[13]) + long(row[14])
                            )

                        # (len(row) == 16) Pending normal aio reads: 0 [0, 0, 0, 0] , aio writes: 0 [0, 0] ,
                        elif self._are_values_numeric(row[4:9]) and self._are_values_numeric(row[12:15]):
                            results['Innodb_pending_normal_aio_reads'] = long(row[4])
                            results['Innodb_pending_normal_aio_writes'] = long(row[12])
                        else:
                            self.log.warning("Can't parse result line %s", line)
                    elif len(row) == 18:
                        # (len(row) == 18) Pending normal aio reads: 0 [0, 0, 0, 0] , aio writes: 0 [0, 0, 0, 0] ,
                        results['Innodb_pending_normal_aio_reads'] = long(row[4])
                        results['Innodb_pending_normal_aio_writes'] = long(row[12])
                    elif len(row) == 22:
                        # (len(row) == 22)
                        # Pending normal aio reads: 0 [0, 0, 0, 0, 0, 0, 0, 0] , aio writes: 0 [0, 0, 0, 0] ,
                        results['Innodb_pending_normal_aio_reads'] = long(row[4])
                        results['Innodb_pending_normal_aio_writes'] = long(row[16])
                except ValueError as e:
                    self.log.warning("Can't parse result line %s: %s", line, e)
            elif line.find('ibuf aio reads') == 0:
                #  ibuf aio reads: 0, log i/o's: 0, sync i/o's: 0
                #  or ibuf aio reads:, log i/o's:, sync i/o's:
                if len(row) == 10:
                    results['Innodb_pending_ibuf_aio_reads'] = long(row[3])
                    results['Innodb_pending_aio_log_ios'] = long(row[6])
                    results['Innodb_pending_aio_sync_ios'] = long(row[9])
                elif len(row) == 7:
                    results['Innodb_pending_ibuf_aio_reads'] = 0
                    results['Innodb_pending_aio_log_ios'] = 0
                    results['Innodb_pending_aio_sync_ios'] = 0
            elif line.find('Pending flushes (fsync)') == 0:
                # Pending flushes (fsync) log: 0; buffer pool: 0
                results['Innodb_pending_log_flushes'] = long(row[4])
                results['Innodb_pending_buffer_pool_flushes'] = long(row[7])

            # INSERT BUFFER AND ADAPTIVE HASH INDEX
            elif line.find('Ibuf for space 0: size ') == 0:
                # Older InnoDB code seemed to be ready for an ibuf per tablespace.  It
                # had two lines in the output.  Newer has just one line, see below.
                # Ibuf for space 0: size 1, free list len 887, seg size 889, is not empty
                # Ibuf for space 0: size 1, free list len 887, seg size 889,
                results['Innodb_ibuf_size'] = long(row[5])
                results['Innodb_ibuf_free_list'] = long(row[9])
                results['Innodb_ibuf_segment_size'] = long(row[12])
            elif line.find('Ibuf: size ') == 0:
                # Ibuf: size 1, free list len 4634, seg size 4636,
                results['Innodb_ibuf_size'] = long(row[2])
                results['Innodb_ibuf_free_list'] = long(row[6])
                results['Innodb_ibuf_segment_size'] = long(row[9])

                if line.find('merges') > -1:
                    results['Innodb_ibuf_merges'] = long(row[10])
            elif line.find(', delete mark ') > 0 and prev_line.find('merged operations:') == 0:
                # Output of show engine innodb status has changed in 5.5
                # merged operations:
                # insert 593983, delete mark 387006, delete 73092
                results['Innodb_ibuf_merged_inserts'] = long(row[1])
                results['Innodb_ibuf_merged_delete_marks'] = long(row[4])
                results['Innodb_ibuf_merged_deletes'] = long(row[6])
                results['Innodb_ibuf_merged'] = (
                    results['Innodb_ibuf_merged_inserts']
                    + results['Innodb_ibuf_merged_delete_marks']
                    + results['Innodb_ibuf_merged_deletes']
                )
            elif line.find(' merged recs, ') > 0:
                # 19817685 inserts, 19817684 merged recs, 3552620 merges
                results['Innodb_ibuf_merged_inserts'] = long(row[0])
                results['Innodb_ibuf_merged'] = long(row[2])
                results['Innodb_ibuf_merges'] = long(row[5])
            elif line.find('Hash table size ') == 0:
                # In some versions of InnoDB, the used cells is omitted.
                # Hash table size 4425293, used cells 4229064, ....
                # Hash table size 57374437, node heap has 72964 buffer(s) <--
                # no used cells
                results['Innodb_hash_index_cells_total'] = long(row[3])
                results['Innodb_hash_index_cells_used'] = long(row[6]) if line.find('used cells') > 0 else 0

            # LOG
            elif line.find(" log i/o's done, ") > 0:
                # 3430041 log i/o's done, 17.44 log i/o's/second
                # 520835887 log i/o's done, 17.28 log i/o's/second, 518724686
                # syncs, 2980893 checkpoints
                results['Innodb_log_writes'] = long(row[0])
            elif line.find(" pending log writes, ") > 0:
                # 0 pending log writes, 0 pending chkp writes
                results['Innodb_pending_log_writes'] = long(row[0])
                results['Innodb_pending_checkpoint_writes'] = long(row[4])
            elif line.find("Log sequence number") == 0:
                # This number is NOT printed in hex in InnoDB plugin.
                # Log sequence number 272588624
                results['Innodb_lsn_current'] = long(row[3])
            elif line.find("Log flushed up to") == 0:
                # This number is NOT printed in hex in InnoDB plugin.
                # Log flushed up to   272588624
                results['Innodb_lsn_flushed'] = long(row[4])
            elif line.find("Last checkpoint at") == 0:
                # Last checkpoint at  272588624
                results['Innodb_lsn_last_checkpoint'] = long(row[3])

            # BUFFER POOL AND MEMORY
            elif line.find("Total memory allocated") == 0 and line.find("in additional pool allocated") > 0:
                # Total memory allocated 29642194944; in additional pool allocated 0
                # Total memory allocated by read views 96
                results['Innodb_mem_total'] = long(row[3])
                results['Innodb_mem_additional_pool'] = long(row[8])
            elif line.find('Adaptive hash index ') == 0:
                #   Adaptive hash index 1538240664     (186998824 + 1351241840)
                results['Innodb_mem_adaptive_hash'] = long(row[3])
            elif line.find('Page hash           ') == 0:
                #   Page hash           11688584
                results['Innodb_mem_page_hash'] = long(row[2])
            elif line.find('Dictionary cache    ') == 0:
                #   Dictionary cache    145525560      (140250984 + 5274576)
                results['Innodb_mem_dictionary'] = long(row[2])
            elif line.find('File system         ') == 0:
                #   File system         313848         (82672 + 231176)
                results['Innodb_mem_file_system'] = long(row[2])
            elif line.find('Lock system         ') == 0:
                #   Lock system         29232616       (29219368 + 13248)
                results['Innodb_mem_lock_system'] = long(row[2])
            elif line.find('Recovery system     ') == 0:
                #   Recovery system     0      (0 + 0)
                results['Innodb_mem_recovery_system'] = long(row[2])
            elif line.find('Threads             ') == 0:
                #   Threads             409336         (406936 + 2400)
                results['Innodb_mem_thread_hash'] = long(row[1])
            elif line.find("Buffer pool size ") == 0:
                # The " " after size is necessary to avoid matching the wrong line:
                # Buffer pool size        1769471
                # Buffer pool size, bytes 28991012864
                if buffer_id == -1:
                    results['Innodb_buffer_pool_pages_total'] = long(row[3])
            elif line.find("Free buffers") == 0:
                # Free buffers            0
                if buffer_id == -1:
                    results['Innodb_buffer_pool_pages_free'] = long(row[2])
            elif line.find("Database pages") == 0:
                # Database pages          1696503
                if buffer_id == -1:
                    results['Innodb_buffer_pool_pages_data'] = long(row[2])

            elif line.find("Modified db pages") == 0:
                # Modified db pages       160602
                if buffer_id == -1:
                    results['Innodb_buffer_pool_pages_dirty'] = long(row[3])
            elif line.find("Pages read ahead") == 0:
                # Must do this BEFORE the next test, otherwise it'll get fooled by this
                # line from the new plugin:
                # Pages read ahead 0.00/s, evicted without access 0.06/s
                pass
            elif line.find("Pages read") == 0:
                # Pages read 15240822, created 1770238, written 21705836
                if buffer_id == -1:
                    results['Innodb_pages_read'] = long(row[2])
                    results['Innodb_pages_created'] = long(row[4])
                    results['Innodb_pages_written'] = long(row[6])

            # ROW OPERATIONS
            elif line.find('Number of rows inserted') == 0:
                # Number of rows inserted 50678311, updated 66425915, deleted
                # 20605903, read 454561562
                results['Innodb_rows_inserted'] = long(row[4])
                results['Innodb_rows_updated'] = long(row[6])
                results['Innodb_rows_deleted'] = long(row[8])
                results['Innodb_rows_read'] = long(row[10])
            elif line.find(" queries inside InnoDB, ") > 0:
                # 0 queries inside InnoDB, 0 queries in queue
                results['Innodb_queries_inside'] = long(row[0])
                results['Innodb_queries_queued'] = long(row[4])

            prev_line = line

        # We need to calculate this metric separately
        try:
            results['Innodb_checkpoint_age'] = results['Innodb_lsn_current'] - results['Innodb_lsn_last_checkpoint']
        except KeyError as e:
            self.log.error("Not all InnoDB LSN metrics available, unable to compute: %s", e)

        # Finally we change back the metrics values to string to make the values
        # consistent with how they are reported by SHOW GLOBAL STATUS
        for metric, value in list(iteritems(results)):
            results[metric] = str(value)

        return results

    def _get_variable_enabled(self, results, var):
        enabled = collect_string(var, results)
        return enabled and enabled.lower().strip() == 'on'

    def _get_query_exec_time_95th_us(self, db):
        # Fetches the 95th percentile query execution time and returns the value
        # in microseconds
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute(SQL_95TH_PERCENTILE)

                if cursor.rowcount < 1:
                    self.warning(
                        "Failed to fetch records from the perf schema \
                                 'events_statements_summary_by_digest' table."
                    )
                    return None

                row = cursor.fetchone()
                query_exec_time_95th_per = row[0]

                return query_exec_time_95th_per
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("95th percentile performance metrics unavailable at this time: %s", e)
            return None

    def _query_exec_time_per_schema(self, db):
        # Fetches the avg query execution time per schema and returns the
        # value in microseconds
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute(SQL_AVG_QUERY_RUN_TIME)

                if cursor.rowcount < 1:
                    self.warning(
                        "Failed to fetch records from the perf schema \
                                 'events_statements_summary_by_digest' table."
                    )
                    return None

                schema_query_avg_run_time = {}
                for row in cursor.fetchall():
                    schema_name = str(row[0])
                    avg_us = long(row[1])

                    # set the tag as the dictionary key
                    schema_query_avg_run_time["schema:{0}".format(schema_name)] = avg_us

                return schema_query_avg_run_time
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("Avg exec time performance metrics unavailable at this time: %s", e)
            return None

    def _query_size_per_schema(self, db):
        # Fetches the avg query execution time per schema and returns the
        # value in microseconds
        try:
            with closing(db.cursor()) as cursor:
                cursor.execute(SQL_QUERY_SCHEMA_SIZE)

                if cursor.rowcount < 1:
                    self.warning("Failed to fetch records from the information schema 'tables' table.")
                    return None

                schema_size = {}
                for row in cursor.fetchall():
                    schema_name = str(row[0])
                    size = long(row[1])

                    # set the tag as the dictionary key
                    schema_size["schema:{0}".format(schema_name)] = size

                return schema_size
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("Avg exec time performance metrics unavailable at this time: %s", e)

        return {}

    def _compute_synthetic_results(self, results):
        if ('Qcache_hits' in results) and ('Qcache_inserts' in results) and ('Qcache_not_cached' in results):
            if not int(results['Qcache_hits']):
                results['Qcache_utilization'] = 0
            else:
                results['Qcache_utilization'] = (
                    float(results['Qcache_hits'])
                    / (int(results['Qcache_inserts']) + int(results['Qcache_not_cached']) + int(results['Qcache_hits']))
                    * 100
                )

            if all(v is not None for v in (self._qcache_hits, self._qcache_inserts, self._qcache_not_cached)):
                if not (int(results['Qcache_hits']) - self._qcache_hits):
                    results['Qcache_instant_utilization'] = 0
                else:
                    top = float(results['Qcache_hits']) - self._qcache_hits
                    bottom = (
                        (int(results['Qcache_inserts']) - self._qcache_inserts)
                        + (int(results['Qcache_not_cached']) - self._qcache_not_cached)
                        + (int(results['Qcache_hits']) - self._qcache_hits)
                    )
                    results['Qcache_instant_utilization'] = (top / bottom) * 100

            # update all three, or none - for consistent samples.
            self._qcache_hits = int(results['Qcache_hits'])
            self._qcache_inserts = int(results['Qcache_inserts'])
            self._qcache_not_cached = int(results['Qcache_not_cached'])
