#!/usr/bin/env python3
"""
Magento 2 Health Monitor (magento-test.py)

Magento 2 / Adobe Commerce counterpart of cloud2.py. Runs the same frontend, PHP
resource, slow log, HTTP error and capacity investigation, and replaces the
WordPress/WooCommerce sections with Magento checks: installation & deploy mode,
database bloat, cache types / FPC / Redis, indexers, cron, message queues,
modules, OpenSearch/Elasticsearch and var/log analysis.

All checks are read-only (no cache flush, reindex or config changes). The report
is saved as a plain-text `.txt` file (Cloudways/nginx block `*.log` URLs) so it
can be opened in a browser and used in SiteSleuth's investigation log field.

Run from the Magento root (public_html on Cloudways) or pass --magento-root.
"""

import subprocess
import json
import time
import re
import statistics
import os
import glob
import shlex
import shutil
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
import requests
from urllib.parse import urljoin

# Color codes for terminal output
class Colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ORANGE = '\033[38;5;214m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


class MagentoHealthMonitor:
    """Main class for Magento 2 health monitoring"""

    def __init__(self, site_url: str, magento_root: str = None):
        self.site_url = site_url.rstrip('/')
        self.magento_root = find_magento_root(magento_root)
        self.is_root = subprocess.run(['id', '-u'], capture_output=True, text=True).stdout.strip() == '0'
        self.report = {}

    def run_magento_command(self, command: str, timeout: int = 60) -> str:
        """Execute bin/magento command with timeout (read-only commands only)"""
        if not self.magento_root:
            return ""
        try:
            full_command = f"php -d memory_limit=-1 bin/magento {command} --no-ansi"
            result = subprocess.run(
                full_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.magento_root
            )
            return (result.stdout or result.stderr).strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            return f"Error: {str(e)}"

    def get_env(self) -> Dict:
        """Load app/etc/env.php as a dict (cached)"""
        global _ENV_CACHE
        if _ENV_CACHE is not None:
            return _ENV_CACHE
        _ENV_CACHE = {}
        if not self.magento_root:
            return _ENV_CACHE
        env_file = os.path.join(self.magento_root, 'app', 'etc', 'env.php')
        try:
            result = subprocess.run(
                ['php', '-r', 'echo json_encode(include $argv[1]);', env_file],
                capture_output=True, text=True, timeout=15
            )
            _ENV_CACHE = json.loads(result.stdout) or {}
        except Exception:
            _ENV_CACHE = {}
        return _ENV_CACHE

    def db_config(self) -> Dict:
        env = self.get_env()
        conn = (((env.get('db') or {}).get('connection') or {}).get('default')) or {}
        prefix = (env.get('db') or {}).get('table_prefix') or ''
        conn = dict(conn)
        conn['table_prefix'] = prefix
        return conn

    def t(self, table: str) -> str:
        """Apply the configured table prefix"""
        return f"{self.db_config().get('table_prefix', '')}{table}"

    def run_sql(self, query: str, timeout: int = 60) -> List[List[str]]:
        """Run a read-only SQL query using env.php credentials, return rows"""
        cfg = self.db_config()
        if not cfg.get('dbname'):
            return []
        host = str(cfg.get('host') or 'localhost')
        port = None
        if ':' in host and not host.startswith('/'):
            host, port = host.split(':', 1)
        cmd = ['mysql', '-N', '-B', '-u', str(cfg.get('username', '')), '-h', host]
        if port:
            cmd += ['-P', port]
        cmd += [str(cfg['dbname']), '-e', query]
        env = dict(os.environ)
        env['MYSQL_PWD'] = str(cfg.get('password') or '')
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            if result.returncode != 0:
                return []
            return [line.split('\t') for line in result.stdout.strip().splitlines() if line]
        except Exception:
            return []

    def sql_value(self, query: str, default=None):
        rows = self.run_sql(query)
        if rows and rows[0]:
            return rows[0][0]
        return default

    def config_value(self, path: str) -> Optional[str]:
        """Read a default-scope value from core_config_data"""
        safe = path.replace("'", "")
        return self.sql_value(
            f"SELECT value FROM {self.t('core_config_data')} WHERE path='{safe}' AND scope='default' LIMIT 1"
        )

    def print_section(self, title: str):
        """Print formatted section header"""
        print(f"\n{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.CYAN}{Colors.BOLD}{title}{Colors.RESET}")
        print(f"{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.RESET}\n")


_ENV_CACHE = None
_MAGENTO_ROOT_OVERRIDE = None


def find_magento_root(explicit: str = None) -> Optional[str]:
    """Locate the Magento root (dir containing bin/magento and app/etc/env.php)"""
    candidates = [explicit, _MAGENTO_ROOT_OVERRIDE, os.getcwd(), os.path.dirname(os.getcwd())]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, 'bin', 'magento')) and os.path.isfile(os.path.join(c, 'app', 'etc', 'env.php')):
            return os.path.abspath(c)
    return None


def _fmt_mb(value) -> str:
    try:
        return f"{float(value):.2f} MB"
    except Exception:
        return "N/A"


class MagentoBackendMetrics(MagentoHealthMonitor):
    """Magento backend, database and application metrics"""

    def check_installation(self) -> Dict:
        print(f"{Colors.CYAN}Checking Magento installation...{Colors.RESET}")
        if not self.magento_root:
            print(f"{Colors.RED}Magento root not found (need bin/magento + app/etc/env.php). "
                  f"Run from public_html or pass --magento-root.{Colors.RESET}")
            return {'found': False}

        version = self.run_magento_command("--version", timeout=30)
        version_match = re.search(r'(\d+\.\d+\.\d+(?:-p\d+)?)', version or '')
        edition = 'Adobe Commerce' if os.path.isdir(os.path.join(self.magento_root, 'vendor', 'magento', 'module-staging')) else 'Magento Open Source'
        mode_out = self.run_magento_command("deploy:mode:show", timeout=30)
        mode_match = re.search(r'mode:\s*(\w+)', mode_out or '')
        mode = mode_match.group(1) if mode_match else (self.get_env().get('MAGE_MODE') or 'unknown')
        php_version = subprocess.run(['php', '-r', 'echo PHP_VERSION;'], capture_output=True, text=True).stdout.strip()

        result = {
            'found': True,
            'root': self.magento_root,
            'version': version_match.group(1) if version_match else 'unknown',
            'edition': edition,
            'deploy_mode': mode,
            'php_version': php_version,
            'status': 'good' if mode == 'production' else 'warning'
        }
        print(f"Root: {self.magento_root}")
        print(f"Version: {result['version']} ({edition})")
        print(f"PHP: {php_version}")
        color = Colors.GREEN if mode == 'production' else Colors.ORANGE
        print(f"{color}Deploy mode: {mode}{Colors.RESET}")
        if mode != 'production':
            print(f"{Colors.ORANGE}  → Non-production mode is significantly slower on live stores{Colors.RESET}")
        return result

    def check_database_size(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking database size...{Colors.RESET}")
        dbname = self.db_config().get('dbname')
        if not dbname:
            print(f"{Colors.YELLOW}Database credentials not available from env.php{Colors.RESET}")
            return {}
        rows = self.run_sql(
            "SELECT ROUND(SUM(data_length+index_length)/1024/1024,2), COUNT(*) "
            f"FROM information_schema.tables WHERE table_schema='{dbname}'"
        )
        if not rows:
            print(f"{Colors.RED}Could not query database (check mysql client / credentials){Colors.RESET}")
            return {}
        size_mb, tables = rows[0][0], rows[0][1]
        top = self.run_sql(
            "SELECT table_name, ROUND((data_length+index_length)/1024/1024,2), table_rows "
            f"FROM information_schema.tables WHERE table_schema='{dbname}' "
            "ORDER BY (data_length+index_length) DESC LIMIT 15"
        )
        size = float(size_mb or 0)
        status = 'critical' if size > 20000 else 'warning' if size > 5000 else 'good'
        color = Colors.RED if status == 'critical' else Colors.ORANGE if status == 'warning' else Colors.GREEN
        print(f"{color}Total DB size: {size:.2f} MB across {tables} tables{Colors.RESET}")
        print(f"\n{Colors.CYAN}Largest tables:{Colors.RESET}")
        print(f"{'Table':<50} {'Size (MB)':<12} {'Rows':<12}")
        for name, mb, rws in top:
            print(f"  {name:<48} {mb:<12} {rws:<12}")
        return {
            'total_size': f"{size:.2f} MB",
            'total_size_mb': size,
            'table_count': int(tables or 0),
            'largest_tables': [{'table': n, 'size_mb': float(m or 0), 'rows': r} for n, m, r in top],
            'status': status
        }

    def check_table_bloat(self) -> Dict:
        """Tables that commonly grow unbounded in Magento"""
        print(f"\n{Colors.CYAN}Checking cleanup candidates (table bloat)...{Colors.RESET}")
        checks = {
            'customer_visitor': ("SELECT COUNT(*) FROM {t}", 500000),
            'report_event': ("SELECT COUNT(*) FROM {t}", 500000),
            'report_viewed_product_index': ("SELECT COUNT(*) FROM {t}", 500000),
            'quote_inactive_30d': ("SELECT COUNT(*) FROM {quote} WHERE is_active=0 AND updated_at < NOW() - INTERVAL 30 DAY", 200000),
            'quote_abandoned_90d': ("SELECT COUNT(*) FROM {quote} WHERE is_active=1 AND updated_at < NOW() - INTERVAL 90 DAY", 200000),
            'url_rewrite': ("SELECT COUNT(*) FROM {t}", 2000000),
            'session': ("SELECT COUNT(*) FROM {t}", 100000),
            'search_query': ("SELECT COUNT(*) FROM {t}", 200000),
            'cron_schedule': ("SELECT COUNT(*) FROM {t}", 50000),
            'queue_message': ("SELECT COUNT(*) FROM {t}", 100000),
            'magento_bulk': ("SELECT COUNT(*) FROM {t}", 100000),
            'catalogsearch_recommendations': ("SELECT COUNT(*) FROM {t}", 100000),
        }
        existing = {r[0] for r in self.run_sql(
            f"SELECT table_name FROM information_schema.tables WHERE table_schema='{self.db_config().get('dbname', '')}'"
        )}
        result = {}
        print(f"{'Table / metric':<35} {'Rows':<12} {'Status':<10}")
        for key, (query, threshold) in checks.items():
            base = 'quote' if key.startswith('quote_') else key
            if self.t(base) not in existing:
                continue
            value = self.sql_value(query.format(t=self.t(key), quote=self.t('quote')), '0')
            try:
                count = int(value)
            except Exception:
                count = 0
            status = 'warning' if count > threshold else 'good'
            color = Colors.ORANGE if status == 'warning' else Colors.GREEN
            print(f"{color}  {key:<33} {count:<12} {status:<10}{Colors.RESET}")
            result[key] = {'rows': count, 'threshold': threshold, 'status': status}
        flagged = [k for k, v in result.items() if v['status'] == 'warning']
        if flagged:
            print(f"\n{Colors.ORANGE}Cleanup recommended: {', '.join(flagged)}{Colors.RESET}")
        return {'tables': result, 'flagged': flagged, 'status': 'warning' if flagged else 'good'}

    def check_database_query_performance(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking database performance...{Colors.RESET}")
        start = time.time()
        rows = self.run_sql(f"SELECT COUNT(*) FROM {self.t('core_config_data')}")
        latency_ms = round((time.time() - start) * 1000, 2)
        if not rows:
            print(f"{Colors.YELLOW}Database not reachable{Colors.RESET}")
            return {}
        status_rows = dict(self.run_sql(
            "SHOW GLOBAL STATUS WHERE Variable_name IN "
            "('Slow_queries','Threads_connected','Threads_running','Uptime','Questions',"
            "'Created_tmp_disk_tables','Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests')"
        ))
        var_rows = dict(self.run_sql(
            "SHOW GLOBAL VARIABLES WHERE Variable_name IN "
            "('innodb_buffer_pool_size','max_connections','long_query_time','slow_query_log','version')"
        ))
        result = {'query_latency_ms': latency_ms}
        try:
            reads = float(status_rows.get('Innodb_buffer_pool_reads', 0))
            reqs = float(status_rows.get('Innodb_buffer_pool_read_requests', 0)) or 1
            result['buffer_pool_hit_rate'] = round((1 - reads / reqs) * 100, 3)
        except Exception:
            pass
        try:
            uptime = float(status_rows.get('Uptime', 0)) or 1
            result['qps'] = round(float(status_rows.get('Questions', 0)) / uptime, 2)
        except Exception:
            pass
        result['slow_queries_total'] = status_rows.get('Slow_queries')
        result['threads_connected'] = status_rows.get('Threads_connected')
        result['threads_running'] = status_rows.get('Threads_running')
        result['tmp_disk_tables'] = status_rows.get('Created_tmp_disk_tables')
        result['mysql_version'] = var_rows.get('version')
        try:
            result['innodb_buffer_pool_size_mb'] = round(int(var_rows.get('innodb_buffer_pool_size', 0)) / 1024 / 1024)
        except Exception:
            pass
        result['max_connections'] = var_rows.get('max_connections')
        result['slow_query_log'] = var_rows.get('slow_query_log')

        color = Colors.RED if latency_ms > 200 else Colors.ORANGE if latency_ms > 50 else Colors.GREEN
        print(f"{color}Simple query latency: {latency_ms} ms{Colors.RESET}")
        for label, key in [('MySQL version', 'mysql_version'), ('Buffer pool size (MB)', 'innodb_buffer_pool_size_mb'),
                           ('Buffer pool hit rate (%)', 'buffer_pool_hit_rate'), ('Queries/sec (avg since start)', 'qps'),
                           ('Slow queries (since start)', 'slow_queries_total'), ('Threads connected', 'threads_connected'),
                           ('Threads running', 'threads_running'), ('Max connections', 'max_connections'),
                           ('Tmp tables on disk', 'tmp_disk_tables')]:
            print(f"  {label}: {result.get(key, 'N/A')}")
        hit = result.get('buffer_pool_hit_rate')
        if hit is not None and hit < 99:
            print(f"{Colors.ORANGE}  → Buffer pool hit rate below 99%: consider a larger innodb_buffer_pool_size{Colors.RESET}")
        result['status'] = 'critical' if latency_ms > 200 else 'warning' if latency_ms > 50 else 'good'
        return result

    def check_memory_usage(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking PHP configuration...{Colors.RESET}")
        try:
            php = subprocess.run(
                ['php', '-r', 'echo json_encode(["memory_limit"=>ini_get("memory_limit"),'
                 '"max_execution_time"=>ini_get("max_execution_time"),'
                 '"opcache"=>function_exists("opcache_get_status")?(bool)@opcache_get_status(false):false,'
                 '"realpath_cache_size"=>ini_get("realpath_cache_size")]);'],
                capture_output=True, text=True, timeout=15
            ).stdout
            data = json.loads(php)
        except Exception:
            data = {}
        mem = str(data.get('memory_limit', ''))
        mem_mb = None
        m = re.match(r'^(-?\d+)([KMG]?)$', mem.upper())
        if m:
            val, unit = int(m.group(1)), m.group(2)
            mem_mb = -1 if val == -1 else val / 1024 if unit == 'K' else val * 1024 if unit == 'G' else val if unit == 'M' else val / 1024 / 1024
        status = 'warning' if mem_mb is not None and 0 < mem_mb < 756 else 'good'
        color = Colors.ORANGE if status == 'warning' else Colors.GREEN
        print(f"{color}CLI memory_limit: {mem or 'N/A'}{Colors.RESET} (Magento recommends >= 756M, 2G for CLI)")
        print(f"  max_execution_time: {data.get('max_execution_time', 'N/A')}")
        print(f"  realpath_cache_size: {data.get('realpath_cache_size', 'N/A')}")
        print("  Note: values are from PHP CLI; FPM pool settings may differ")
        return {'memory_limit': mem, 'memory_limit_mb': mem_mb, **data, 'status': status}


class MagentoCacheMetrics(MagentoHealthMonitor):
    """Cache types, full page cache and backend storage"""

    def check_cache_status(self) -> Dict:
        print(f"{Colors.CYAN}Checking cache types...{Colors.RESET}")
        out = self.run_magento_command("cache:status")
        types = {}
        for line in (out or '').splitlines():
            m = re.match(r'^\s*([a-z_]+):\s*([01])\s*$', line)
            if m:
                types[m.group(1)] = m.group(2) == '1'
        if not types:
            env_types = self.get_env().get('cache_types') or {}
            types = {k: bool(v) for k, v in env_types.items()}
        disabled = [k for k, v in types.items() if not v]
        for name, enabled in types.items():
            color = Colors.GREEN if enabled else Colors.RED
            print(f"{color}  {name:<30} {'enabled' if enabled else 'DISABLED'}{Colors.RESET}")
        if disabled:
            print(f"\n{Colors.RED}Disabled cache types: {', '.join(disabled)}{Colors.RESET}")
        return {'types': types, 'disabled': disabled,
                'status': 'critical' if 'full_page' in disabled or 'config' in disabled else 'warning' if disabled else 'good'}

    def check_full_page_cache(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking Full Page Cache...{Colors.RESET}")
        app = self.config_value('system/full_page_cache/caching_application') or '1'
        engine = 'Varnish' if app == '2' else 'Built-in'
        ttl = self.config_value('system/full_page_cache/ttl') or '86400'
        result = {'application': engine, 'ttl': ttl}
        hits = 0
        headers_seen = {}
        try:
            for _ in range(3):
                r = requests.get(self.site_url, timeout=20, headers={'User-Agent': 'Mozilla/5.0 MagentoHealthMonitor'})
                h = {k.lower(): v for k, v in r.headers.items()}
                for key in ('x-magento-cache-debug', 'x-cache', 'x-varnish', 'age', 'x-magento-tags', 'cache-control', 'x-cache-status'):
                    if key in h:
                        headers_seen[key] = h[key]
                if 'hit' in (h.get('x-magento-cache-debug', '') + h.get('x-cache', '') + h.get('x-cache-status', '')).lower():
                    hits += 1
                elif h.get('age', '0') not in ('', '0'):
                    hits += 1
        except Exception as e:
            result['error'] = str(e)
        result['homepage_cache_hits'] = f"{hits}/3"
        result['headers'] = headers_seen
        print(f"FPC application: {engine} (TTL {ttl}s)")
        color = Colors.GREEN if hits >= 2 else Colors.ORANGE if hits == 1 else Colors.RED
        print(f"{color}Homepage cache hits on repeat requests: {hits}/3{Colors.RESET}")
        for k, v in headers_seen.items():
            print(f"  {k}: {v[:100]}")
        if hits == 0:
            print(f"{Colors.ORANGE}  → Homepage not served from cache: check FPC, cookies, uncacheable blocks (cacheable=\"false\"){Colors.RESET}")
        if engine == 'Built-in':
            print(f"{Colors.ORANGE}  → Built-in FPC in use; Varnish is recommended for production{Colors.RESET}")
        result['status'] = 'good' if hits >= 2 else 'warning'
        return result

    def check_cache_backends(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking cache & session storage...{Colors.RESET}")
        env = self.get_env()
        frontend = ((env.get('cache') or {}).get('frontend')) or {}

        def backend_name(cfg):
            b = (cfg or {}).get('backend', 'file')
            if 'Redis' in b or 'redis' in b.lower():
                return 'redis'
            return 'file' if b in ('', 'file', 'Cm_Cache_Backend_File') else b

        default_cache = backend_name(frontend.get('default'))
        page_cache = backend_name(frontend.get('page_cache'))
        session = (env.get('session') or {}).get('save', 'files')
        result = {'default_cache': default_cache, 'page_cache': page_cache, 'session': session}
        for label, val in [('Default cache', default_cache), ('Page cache', page_cache), ('Sessions', session)]:
            good = val in ('redis', 'db') if label == 'Sessions' else val == 'redis'
            color = Colors.GREEN if good else Colors.ORANGE
            print(f"{color}  {label:<15} {val}{Colors.RESET}")

        redis_cfg = ((frontend.get('default') or {}).get('backend_options')) or {}
        if default_cache == 'redis' and redis_cfg.get('server'):
            info = self._redis_info(str(redis_cfg.get('server')), str(redis_cfg.get('port', 6379)), redis_cfg.get('password'))
            if info:
                result['redis'] = info
                print(f"  Redis/Valkey {info.get('redis_version', '?')}: used {info.get('used_memory_human', '?')} "
                      f"/ max {info.get('maxmemory_human', '?')}, policy {info.get('maxmemory_policy', '?')}, "
                      f"evicted {info.get('evicted_keys', '?')}")
                if info.get('maxmemory_policy') == 'noeviction':
                    print(f"{Colors.ORANGE}  → maxmemory-policy noeviction can cause write errors when full{Colors.RESET}")
        if default_cache == 'file':
            print(f"{Colors.ORANGE}  → File cache backend: Redis/Valkey recommended for production{Colors.RESET}")
            try:
                size = subprocess.run(['du', '-sm', os.path.join(self.magento_root or '.', 'var', 'cache')],
                                      capture_output=True, text=True, timeout=60).stdout.split()[0]
                result['var_cache_mb'] = size
                print(f"  var/cache size: {size} MB")
            except Exception:
                pass
        result['status'] = 'good' if default_cache == 'redis' else 'warning'
        return result

    def _redis_info(self, host: str, port: str, password=None) -> Dict:
        cli = shutil.which('redis-cli') or shutil.which('valkey-cli')
        if not cli:
            return {}
        cmd = [cli]
        if host.startswith('/'):
            cmd += ['-s', host]
        else:
            cmd += ['-h', host, '-p', port]
        env = dict(os.environ)
        if password:
            env['REDISCLI_AUTH'] = str(password)
        try:
            out = subprocess.run(cmd + ['INFO'], capture_output=True, text=True, timeout=10, env=env).stdout
        except Exception:
            return {}
        info = {}
        for line in out.splitlines():
            if ':' in line and not line.startswith('#'):
                k, v = line.split(':', 1)
                if k in ('redis_version', 'valkey_version', 'used_memory_human', 'maxmemory_human',
                         'maxmemory_policy', 'evicted_keys', 'keyspace_hits', 'keyspace_misses', 'connected_clients'):
                    info[k] = v.strip()
        if 'valkey_version' in info and 'redis_version' not in info:
            info['redis_version'] = info['valkey_version']
        return info


class MagentoIndexerCron(MagentoHealthMonitor):
    """Indexers and cron health"""

    def check_indexers(self) -> Dict:
        print(f"{Colors.CYAN}Checking indexers...{Colors.RESET}")
        status_out = self.run_magento_command("indexer:status", timeout=120)
        mode_out = self.run_magento_command("indexer:show-mode", timeout=60)
        indexers = {}
        for line in (status_out or '').splitlines():
            if '|' in line:
                cols = [c.strip() for c in line.strip('|').split('|')]
                if len(cols) >= 3 and cols[0] and cols[0] not in ('ID', 'Title') and not set(cols[0]) <= set('-+'):
                    indexers[cols[0]] = {'title': cols[1], 'status': cols[2],
                                         'mode': cols[3] if len(cols) > 3 else '',
                                         'backlog': cols[5] if len(cols) > 5 else ''}
            else:
                m = re.match(r'^(.+?):\s*(Ready|Reindex required|Processing|Suspended|Valid|Invalid)', line.strip())
                if m:
                    indexers[m.group(1)] = {'title': m.group(1), 'status': m.group(2), 'mode': '', 'backlog': ''}
        for line in (mode_out or '').splitlines():
            m = re.match(r'^(.+?):\s*(Update on Save|Update by Schedule)', line.strip())
            if m:
                for v in indexers.values():
                    if v['title'] == m.group(1) and not v['mode']:
                        v['mode'] = m.group(2)
        invalid = [k for k, v in indexers.items() if 'required' in v['status'].lower() or 'invalid' in v['status'].lower()]
        on_save = [k for k, v in indexers.items() if 'save' in v['mode'].lower()]
        for key, v in indexers.items():
            color = Colors.RED if key in invalid else Colors.ORANGE if key in on_save else Colors.GREEN
            backlog = f" backlog={v['backlog']}" if v['backlog'] else ''
            print(f"{color}  {v['title'][:40]:<40} {v['status']:<18} {v['mode']}{backlog}{Colors.RESET}")
        if invalid:
            print(f"\n{Colors.RED}Indexers needing reindex: {', '.join(invalid)}{Colors.RESET}")
        if on_save:
            print(f"{Colors.ORANGE}Indexers on 'Update on Save' (slow admin saves; 'Update by Schedule' recommended): {len(on_save)}{Colors.RESET}")
        if not indexers:
            print(f"{Colors.YELLOW}Could not read indexer status{Colors.RESET}")
        return {'indexers': indexers, 'invalid': invalid, 'update_on_save': on_save,
                'status': 'critical' if invalid else 'warning' if on_save else 'good'}

    def check_cron(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking cron (cron_schedule)...{Colors.RESET}")
        t = self.t('cron_schedule')
        by_status = dict(self.run_sql(f"SELECT status, COUNT(*) FROM {t} GROUP BY status"))
        last_success = self.sql_value(f"SELECT MAX(finished_at) FROM {t} WHERE status='success'")
        minutes_since = self.sql_value(
            f"SELECT TIMESTAMPDIFF(MINUTE, MAX(finished_at), UTC_TIMESTAMP()) FROM {t} WHERE status='success'")
        pending_overdue = self.sql_value(
            f"SELECT COUNT(*) FROM {t} WHERE status='pending' AND scheduled_at < UTC_TIMESTAMP() - INTERVAL 1 HOUR", '0')
        stuck_running = self.sql_value(
            f"SELECT COUNT(*) FROM {t} WHERE status='running' AND executed_at < UTC_TIMESTAMP() - INTERVAL 2 HOUR", '0')
        top_errors = self.run_sql(
            f"SELECT job_code, COUNT(*), LEFT(REPLACE(MAX(messages), '\\n', ' '), 150) FROM {t} "
            "WHERE status IN ('error','missed') GROUP BY job_code ORDER BY COUNT(*) DESC LIMIT 10")
        slowest = self.run_sql(
            f"SELECT job_code, ROUND(AVG(TIMESTAMPDIFF(SECOND, executed_at, finished_at))), "
            f"MAX(TIMESTAMPDIFF(SECOND, executed_at, finished_at)) FROM {t} "
            "WHERE status='success' AND executed_at IS NOT NULL GROUP BY job_code "
            "ORDER BY 3 DESC LIMIT 10")
        if not by_status and last_success is None:
            print(f"{Colors.YELLOW}cron_schedule not readable{Colors.RESET}")
            return {}
        try:
            mins = int(minutes_since) if minutes_since not in (None, 'NULL') else None
        except Exception:
            mins = None
        cron_running = mins is not None and mins <= 15
        color = Colors.GREEN if cron_running else Colors.RED
        print(f"{color}Last successful cron job: {last_success} UTC ({mins if mins is not None else '?'} min ago){Colors.RESET}")
        if not cron_running:
            print(f"{Colors.RED}  → Cron appears not to be running (no success in last 15 min){Colors.RESET}")
        print("Jobs by status: " + ', '.join(f"{k}={v}" for k, v in by_status.items()))
        print(f"Overdue pending (>1h): {pending_overdue}")
        print(f"Stuck running (>2h): {stuck_running}")
        if top_errors:
            print(f"\n{Colors.CYAN}Top failing/missed jobs:{Colors.RESET}")
            for row in top_errors:
                msg = row[2] if len(row) > 2 and row[2] != 'NULL' else ''
                print(f"{Colors.ORANGE}  {row[0]:<45} {row[1]:<6} {msg[:90]}{Colors.RESET}")
        if slowest:
            print(f"\n{Colors.CYAN}Slowest cron jobs (avg / max seconds):{Colors.RESET}")
            for row in slowest:
                print(f"  {row[0]:<45} {row[1]:<8} {row[2]}")
        errors = int(by_status.get('error', 0) or 0) + int(by_status.get('missed', 0) or 0)
        status = 'critical' if not cron_running else 'warning' if errors > 50 or int(pending_overdue or 0) > 100 else 'good'
        return {'by_status': by_status, 'last_success': last_success, 'minutes_since_success': mins,
                'cron_running': cron_running, 'pending_overdue': int(pending_overdue or 0),
                'stuck_running': int(stuck_running or 0),
                'top_errors': [{'job_code': r[0], 'count': r[1]} for r in top_errors],
                'slowest_jobs': [{'job_code': r[0], 'avg_s': r[1], 'max_s': r[2]} for r in slowest],
                'status': status}

    def check_message_queues(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking message queues...{Colors.RESET}")
        env = self.get_env()
        amqp = bool(((env.get('queue') or {}).get('amqp') or {}).get('host'))
        consumers_cron = (env.get('cron_consumers_runner') or {}).get('cron_run', True)
        new_msgs = self.run_sql(
            f"SELECT q.name, COUNT(*) FROM {self.t('queue_message_status')} s "
            f"JOIN {self.t('queue')} q ON q.id=s.queue_id WHERE s.status IN (2,3) "
            "GROUP BY q.name ORDER BY 2 DESC LIMIT 10")
        print(f"Broker: {'RabbitMQ/AMQP' if amqp else 'MySQL (db)'}")
        print(f"Consumers via cron: {consumers_cron}")
        total = 0
        if new_msgs:
            print(f"{Colors.CYAN}Unprocessed messages per queue:{Colors.RESET}")
            for name, cnt in new_msgs:
                total += int(cnt or 0)
                color = Colors.ORANGE if int(cnt or 0) > 1000 else Colors.GREEN
                print(f"{color}  {name:<45} {cnt}{Colors.RESET}")
        else:
            print(f"{Colors.GREEN}No MySQL queue backlog{Colors.RESET}")
        return {'amqp': amqp, 'consumers_cron_run': consumers_cron, 'backlog_total': total,
                'backlog': [{'queue': n, 'count': c} for n, c in new_msgs],
                'status': 'warning' if total > 5000 else 'good'}


class MagentoModulesSearch(MagentoHealthMonitor):
    """Modules, search engine and application logs"""

    def check_modules(self) -> Dict:
        print(f"{Colors.CYAN}Checking modules...{Colors.RESET}")
        config_file = os.path.join(self.magento_root or '.', 'app', 'etc', 'config.php')
        modules = {}
        try:
            out = subprocess.run(['php', '-r', 'echo json_encode((include $argv[1])["modules"] ?? []);', config_file],
                                 capture_output=True, text=True, timeout=15).stdout
            modules = json.loads(out) or {}
        except Exception:
            pass
        enabled = [m for m, v in modules.items() if int(v) == 1]
        disabled = [m for m, v in modules.items() if int(v) == 0]
        core_prefixes = ('Magento_', 'PayPal_', 'Klarna_', 'Amazon_', 'Vertex_', 'Yotpo_', 'dotdigitalgroup_',
                         'Temando_', 'MSP_', 'Dotdigitalgroup_', 'Braintree_', 'PaypalGraphQl', 'Adobe')
        third_party = sorted(m for m in enabled if not m.startswith(core_prefixes))
        app_code = []
        code_dir = os.path.join(self.magento_root or '.', 'app', 'code')
        if os.path.isdir(code_dir):
            for vendor in sorted(os.listdir(code_dir)):
                vdir = os.path.join(code_dir, vendor)
                if os.path.isdir(vdir):
                    app_code += [f"{vendor}_{m}" for m in sorted(os.listdir(vdir)) if os.path.isdir(os.path.join(vdir, m))]
        vendors = defaultdict(int)
        for m in third_party:
            vendors[m.split('_')[0]] += 1
        print(f"Enabled modules: {len(enabled)} (disabled: {len(disabled)})")
        color = Colors.ORANGE if len(third_party) > 60 else Colors.GREEN
        print(f"{color}Third-party modules enabled: {len(third_party)}{Colors.RESET}")
        if vendors:
            print(f"{Colors.CYAN}Third-party vendors:{Colors.RESET}")
            for v, c in sorted(vendors.items(), key=lambda x: x[1], reverse=True)[:20]:
                print(f"  {v:<30} {c} module(s)")
        if app_code:
            print(f"{Colors.CYAN}Custom modules in app/code: {len(app_code)}{Colors.RESET}")
            for m in app_code[:30]:
                print(f"  {m}")
        return {'enabled_count': len(enabled), 'disabled_count': len(disabled),
                'third_party': third_party, 'third_party_count': len(third_party),
                'app_code_modules': app_code, 'vendors': dict(vendors),
                'status': 'warning' if len(third_party) > 60 else 'good'}

    def check_search_engine(self) -> Dict:
        print(f"\n{Colors.CYAN}Checking search engine...{Colors.RESET}")
        env_sys = (((self.get_env().get('system') or {}).get('default') or {}).get('catalog') or {}).get('search') or {}
        engine = env_sys.get('engine') or self.config_value('catalog/search/engine') or 'unknown'
        prefix = 'opensearch' if 'opensearch' in engine else 'elasticsearch7' if 'elasticsearch' in engine else engine
        host = env_sys.get(f'{prefix}_server_hostname') or self.config_value(f'catalog/search/{prefix}_server_hostname') or 'localhost'
        port = env_sys.get(f'{prefix}_server_port') or self.config_value(f'catalog/search/{prefix}_server_port') or '9200'
        index_prefix = env_sys.get(f'{prefix}_index_prefix') or self.config_value(f'catalog/search/{prefix}_index_prefix') or 'magento2'
        result = {'engine': engine, 'host': host, 'port': port, 'index_prefix': index_prefix}
        print(f"Engine: {engine} @ {host}:{port} (index prefix: {index_prefix})")
        base = host if str(host).startswith('http') else f"http://{host}"
        base = f"{base.rstrip('/')}:{port}"
        try:
            health = requests.get(f"{base}/_cluster/health", timeout=10).json()
            result['cluster_status'] = health.get('status')
            color = Colors.GREEN if health.get('status') == 'green' else Colors.ORANGE if health.get('status') == 'yellow' else Colors.RED
            print(f"{color}Cluster health: {health.get('status')} (nodes {health.get('number_of_nodes')}, "
                  f"unassigned shards {health.get('unassigned_shards')}){Colors.RESET}")
            if health.get('status') == 'yellow' and health.get('number_of_nodes') == 1:
                print("  (yellow is normal on a single-node cluster with replicas configured)")
            idx = requests.get(f"{base}/_cat/indices/{index_prefix}*?format=json&bytes=mb", timeout=10).json()
            result['indices'] = [{'index': i.get('index'), 'docs': i.get('docs.count'), 'size_mb': i.get('store.size')} for i in idx]
            for i in result['indices'][:10]:
                print(f"  {i['index']:<45} docs={i['docs']:<10} {i['size_mb']} MB")
            start = time.time()
            requests.get(f"{base}/{index_prefix}*/_search?size=1", timeout=10)
            result['search_latency_ms'] = round((time.time() - start) * 1000, 2)
            print(f"Search round-trip: {result['search_latency_ms']} ms")
            result['status'] = 'critical' if health.get('status') == 'red' else 'good'
        except Exception as e:
            print(f"{Colors.RED}Search engine not reachable at {base}: {str(e)[:120]}{Colors.RESET}")
            result['error'] = str(e)
            result['status'] = 'critical'
        return result

    def check_application_logs(self, tail_lines: int = 5000) -> Dict:
        print(f"\n{Colors.CYAN}Analyzing Magento application logs (var/log)...{Colors.RESET}")
        log_dir = os.path.join(self.magento_root or '.', 'var', 'log')
        result = {}
        for name in ('exception.log', 'system.log', 'debug.log', 'support_report.log', 'cron.log'):
            path = os.path.join(log_dir, name)
            if not os.path.isfile(path):
                continue
            size_mb = round(os.path.getsize(path) / 1024 / 1024, 2)
            try:
                lines = subprocess.run(['tail', '-n', str(tail_lines), path], capture_output=True,
                                       text=True, errors='ignore', timeout=30).stdout.splitlines()
            except Exception:
                lines = []
            levels = defaultdict(int)
            messages = defaultdict(int)
            cutoff = datetime.now() - timedelta(days=7)
            recent = 0
            for line in lines:
                m = re.match(r'^\[(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})[^\]]*\]\s+\S+\.(\w+):\s*(.*)', line)
                if not m:
                    continue
                try:
                    if datetime.strptime(m.group(1), '%Y-%m-%d') >= cutoff:
                        recent += 1
                except Exception:
                    pass
                levels[m.group(3).upper()] += 1
                msg = re.sub(r'\d+', 'N', m.group(4))[:140]
                messages[msg] += 1
            top = sorted(messages.items(), key=lambda x: x[1], reverse=True)[:8]
            result[name] = {'size_mb': size_mb, 'entries_in_tail': sum(levels.values()),
                            'entries_last_7d': recent, 'levels': dict(levels),
                            'top_messages': [{'message': k, 'count': v} for k, v in top]}
            color = Colors.RED if levels.get('CRITICAL', 0) + levels.get('ERROR', 0) > 50 else Colors.ORANGE if levels else Colors.GREEN
            print(f"{color}{name}: {size_mb} MB, {sum(levels.values())} entries in last {tail_lines} lines "
                  f"({recent} in last 7d) {dict(levels)}{Colors.RESET}")
            for msg, cnt in top[:5]:
                print(f"    {cnt:>5}x {msg}")
            if size_mb > 500:
                print(f"{Colors.ORANGE}    → Log is very large; consider rotation{Colors.RESET}")
        if not result:
            print(f"{Colors.YELLOW}No Magento logs found in {log_dir}{Colors.RESET}")
        crit = sum(v['levels'].get('CRITICAL', 0) for v in result.values())
        result['status'] = 'warning' if crit > 20 else 'good'
        result['critical_total'] = crit
        return result


class FrontendMetrics(MagentoHealthMonitor):
    """Frontend performance metrics"""
    
    def measure_ttfb(self, runs: int = 5) -> Dict:
        """Measure Time to First Byte"""
        print(f"{Colors.CYAN}Measuring TTFB (Time to First Byte)...{Colors.RESET}")
        ttfb_values = []
        
        for i in range(runs):
            try:
                start = time.time()
                response = requests.get(self.site_url, timeout=30, stream=True)
                # Get first byte
                next(response.iter_content(chunk_size=1))
                ttfb = (time.time() - start) * 1000  # Convert to ms
                ttfb_values.append(ttfb)
                time.sleep(0.5)  # Brief pause between requests
            except Exception as e:
                print(f"{Colors.RED}Error measuring TTFB: {e}{Colors.RESET}")
        
        if ttfb_values:
            avg_ttfb = statistics.mean(ttfb_values)
            min_ttfb = min(ttfb_values)
            max_ttfb = max(ttfb_values)
            
            status = Colors.GREEN if avg_ttfb < 600 else Colors.ORANGE if avg_ttfb < 1000 else Colors.RED
            
            result = {
                'average_ms': round(avg_ttfb, 2),
                'min_ms': round(min_ttfb, 2),
                'max_ms': round(max_ttfb, 2),
                'samples': runs,
                'status': 'good' if avg_ttfb < 600 else 'warning' if avg_ttfb < 1000 else 'critical'
            }
            
            print(f"{status}Average TTFB: {result['average_ms']}ms{Colors.RESET}")
            print(f"Min: {result['min_ms']}ms | Max: {result['max_ms']}ms")
            print(f"Threshold: <600ms (Good), <1000ms (Warning), >=1000ms (Critical)")
            
            return result
        return {}
    
    def measure_fcp_and_page_load(self) -> Dict:
        """Measure First Contentful Paint and Page Load Time"""
        print(f"\n{Colors.CYAN}Measuring FCP and Page Load Time...{Colors.RESET}")
        
        try:
            start = time.time()
            response = requests.get(self.site_url, timeout=30)
            page_load_time = (time.time() - start) * 1000
            
            # Estimate FCP (typically 60-80% of page load)
            estimated_fcp = page_load_time * 0.7
            
            page_status = Colors.GREEN if page_load_time < 3000 else Colors.ORANGE if page_load_time < 5000 else Colors.RED
            fcp_status = Colors.GREEN if estimated_fcp < 1800 else Colors.ORANGE if estimated_fcp < 3000 else Colors.RED
            
            result = {
                'page_load_ms': round(page_load_time, 2),
                'estimated_fcp_ms': round(estimated_fcp, 2),
                'page_load_status': 'good' if page_load_time < 3000 else 'warning' if page_load_time < 5000 else 'critical',
                'fcp_status': 'good' if estimated_fcp < 1800 else 'warning' if estimated_fcp < 3000 else 'critical'
            }
            
            print(f"{page_status}Page Load Time: {result['page_load_ms']}ms{Colors.RESET}")
            print(f"{fcp_status}Estimated FCP: {result['estimated_fcp_ms']}ms{Colors.RESET}")
            print(f"Thresholds - FCP: <1800ms (Good), Page Load: <3000ms (Good)")
            
            return result
        except Exception as e:
            print(f"{Colors.RED}Error measuring page metrics: {e}{Colors.RESET}")
            return {}
    
    def measure_page_size(self) -> Dict:
        """Measure page size and request count"""
        print(f"\n{Colors.CYAN}Analyzing Page Size and Resources...{Colors.RESET}")
        
        try:
            response = requests.get(self.site_url, timeout=30)
            page_size_bytes = len(response.content)
            page_size_mb = page_size_bytes / (1024 * 1024)
            
            # Count resource links in HTML
            html = response.text
            css_count = len(re.findall(r'<link[^>]*rel=["\']stylesheet["\']', html))
            js_count = len(re.findall(r'<script[^>]*src=', html))
            img_count = len(re.findall(r'<img[^>]*src=', html))
            total_resources = css_count + js_count + img_count + 1  # +1 for HTML
            
            size_status = Colors.GREEN if page_size_mb < 2 else Colors.ORANGE if page_size_mb < 3 else Colors.RED
            resource_status = Colors.GREEN if total_resources < 50 else Colors.ORANGE if total_resources < 100 else Colors.RED
            
            result = {
                'page_size_kb': round(page_size_bytes / 1024, 2),
                'page_size_mb': round(page_size_mb, 2),
                'css_files': css_count,
                'js_files': js_count,
                'images': img_count,
                'total_resources': total_resources,
                'size_status': 'good' if page_size_mb < 2 else 'warning' if page_size_mb < 3 else 'critical',
                'resource_status': 'good' if total_resources < 50 else 'warning' if total_resources < 100 else 'critical'
            }
            
            print(f"{size_status}Page Size: {result['page_size_mb']}MB ({result['page_size_kb']}KB){Colors.RESET}")
            print(f"{resource_status}Total Resources: {result['total_resources']}{Colors.RESET}")
            print(f"  - CSS Files: {css_count}")
            print(f"  - JS Files: {js_count}")
            print(f"  - Images: {img_count}")
            print(f"Thresholds - Size: <2MB (Good), Resources: <50 (Good)")
            
            return result
        except Exception as e:
            print(f"{Colors.RED}Error analyzing page: {e}{Colors.RESET}")
            return {}
    
    def measure_throughput(self, duration: int = 10, concurrent: int = 5) -> Dict:
        """Measure requests per second (throughput)"""
        print(f"\n{Colors.CYAN}Measuring Throughput (Requests/Second)...{Colors.RESET}")
        print(f"Testing with {concurrent} concurrent requests for {duration} seconds...")
        
        import threading
        
        request_count = 0
        errors = 0
        lock = threading.Lock()
        start_time = time.time()
        
        def make_request():
            nonlocal request_count, errors
            while time.time() - start_time < duration:
                try:
                    response = requests.get(self.site_url, timeout=10)
                    with lock:
                        if response.status_code == 200:
                            request_count += 1
                        else:
                            errors += 1
                except:
                    with lock:
                        errors += 1
                time.sleep(0.1)
        
        threads = []
        for _ in range(concurrent):
            t = threading.Thread(target=make_request)
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join()
        
        elapsed = time.time() - start_time
        rps = request_count / elapsed if elapsed > 0 else 0
        error_rate = (errors / (request_count + errors) * 100) if (request_count + errors) > 0 else 0
        
        status = Colors.GREEN if rps > 10 else Colors.ORANGE if rps > 5 else Colors.RED
        
        result = {
            'requests_per_second': round(rps, 2),
            'total_requests': request_count,
            'errors': errors,
            'error_rate_percent': round(error_rate, 2),
            'test_duration_seconds': duration,
            'concurrent_users': concurrent,
            'status': 'good' if rps > 10 else 'warning' if rps > 5 else 'critical'
        }
        
        print(f"{status}Throughput: {result['requests_per_second']} req/sec{Colors.RESET}")
        print(f"Successful: {request_count} | Errors: {errors} | Error Rate: {result['error_rate_percent']}%")
        
        return result



class SlowLogAnalyzer(MagentoHealthMonitor):
    """Analyze PHP slow logs"""
    
    def __init__(self, site_url: str, magento_root: str = None, log_path: str = None):
        super().__init__(site_url, magento_root)
        self.log_path = log_path or "../logs"
    
    def analyze_slow_logs(self, days: int = 7, top_n: int = 10) -> Dict:
        """Analyze PHP slow logs to find slowest scripts"""
        print(f"{Colors.CYAN}Analyzing PHP Slow Logs (Last {days} days)...{Colors.RESET}")
        
        try:
            # Find slow log files
            patterns = [
                f"{self.log_path}/php-app.slow.log*",
                f"{self.log_path}/*slow.log*",
                f"{self.log_path}/php*.slow.log*"
            ]
            
            slow_log_files = []
            for pattern in patterns:
                found = glob.glob(pattern)
                slow_log_files.extend(found)
            
            slow_log_files = list(set(slow_log_files))
            
            if not slow_log_files:
                print(f"{Colors.YELLOW}No slow log files found{Colors.RESET}")
                return {}
            
            print(f"Found {len(slow_log_files)} slow log files")
            
            # Parse slow logs
            slow_requests = defaultdict(
                lambda: {'count': 0, 'total_time': 0.0, 'max_time': 0.0, 'timed_count': 0}
            )
            plugin_trace_hits = defaultdict(int)
            plugin_entry_counts = defaultdict(int)
            plugin_function_counts = defaultdict(lambda: defaultdict(int))
            theme_counts = defaultdict(int)
            function_counts = defaultdict(int)
            source_counts = defaultdict(int)
            cutoff_date = datetime.now() - timedelta(days=days)
            
            date_patterns = [
                (re.compile(r'\[(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2})\]'), '%d-%b-%Y %H:%M:%S'),
                (re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]'), '%Y-%m-%d %H:%M:%S'),
                (re.compile(r'\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2})'), '%d/%b/%Y:%H:%M:%S'),
            ]
            
            duration_patterns = [
                re.compile(r'duration[:=]\s*(\d+(?:\.\d+)?)\s*(ms|msec|s|sec)', re.IGNORECASE),
                re.compile(r'executed\s+in\s*(\d+(?:\.\d+)?)\s*(ms|msec|s|sec)', re.IGNORECASE),
                re.compile(r'(\d+(?:\.\d+)?)\s*(ms|msec|s|sec)\b', re.IGNORECASE),
            ]
            
            def parse_date(line: str) -> Optional[datetime]:
                for regex, fmt in date_patterns:
                    match = regex.search(line)
                    if match:
                        try:
                            return datetime.strptime(match.group(1), fmt)
                        except Exception:
                            continue
                return None
            
            def parse_duration(line: str) -> Optional[float]:
                for regex in duration_patterns:
                    match = regex.search(line)
                    if match:
                        try:
                            value = float(match.group(1))
                            unit = match.group(2).lower()
                            return value / 1000 if unit in ('ms', 'msec') else value
                        except Exception:
                            return None
                return None
            
            def parse_script(line: str) -> Optional[str]:
                match = re.search(r'(?:script_filename|script)\s*=\s*(\S+)', line, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                return None
            
            def parse_trace_details(line: str) -> Tuple[Optional[str], Optional[str]]:
                match = re.search(r'\]\s+([^\s]+)\s*\([^)]*\)\s+(\S+\.php):\d+', line)
                if match:
                    return match.group(1).strip(), match.group(2).strip()
                return None, None

            def parse_trace_function(line: str) -> Optional[str]:
                match = re.search(r'\]\s+([^\s]+)\s*\(', line)
                if match:
                    return match.group(1).strip()
                return None
            
            def parse_trace_path(line: str) -> Optional[str]:
                match = re.search(r'(\S+\.php):\d+', line)
                if match:
                    return match.group(1).strip()
                return None
            
            def extract_plugin(path: str) -> Optional[str]:
                if not path:
                    return None
                # Magento module: app/code/Vendor/Module or vendor/<vendor>/<package> (non-core)
                m = re.search(r'/app/code/([^/]+)/([^/]+)/', path)
                if m:
                    return f"{m.group(1)}_{m.group(2)}"
                m = re.search(r'/vendor/([^/]+)/([^/]+)/', path)
                if m and m.group(1) not in ('magento', 'laminas', 'symfony', 'composer', 'monolog', 'colinmollenhour', 'guzzlehttp', 'psr'):
                    return f"{m.group(1)}/{m.group(2)}"
                return None
            
            def categorize_path(path: str):
                plugin = extract_plugin(path)
                if plugin:
                    plugin_trace_hits[plugin] += 1
                    source_counts['plugins'] += 1
                    return
                if '/app/design/' in path:
                    theme_match = re.search(r'/app/design/[^/]+/([^/]+/[^/]+)/', path)
                    if theme_match:
                        theme = theme_match.group(1)
                        theme_counts[theme] += 1
                        source_counts['themes'] += 1
                        return
                if '/vendor/magento/' in path or '/generated/' in path or '/lib/internal/' in path:
                    source_counts['core'] += 1
                    return
                source_counts['other'] += 1
            
            def record_entry(entry):
                script = entry.get('script')
                if not script:
                    return
                entry_date = entry.get('date')
                if entry_date and entry_date < cutoff_date:
                    return
                data = slow_requests[script]
                data['count'] += 1
                duration = entry.get('duration')
                if duration is not None:
                    data['timed_count'] += 1
                    data['total_time'] += duration
                    data['max_time'] = max(data['max_time'], duration)
                plugins = entry.get('plugins') or set()
                for plugin in plugins:
                    plugin_entry_counts[plugin] += 1
            
            for log_file in slow_log_files:
                try:
                    try:
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
                        if file_mtime < cutoff_date - timedelta(days=1):
                            continue
                    except Exception:
                        pass
                    
                    if log_file.endswith('.gz'):
                        import gzip
                        f = gzip.open(log_file, 'rt', errors='ignore')
                    else:
                        f = open(log_file, 'r', errors='ignore')
                    
                    current_entry = {'date': None, 'script': None, 'duration': None, 'plugins': set()}
                    
                    for line in f:
                        header_date = parse_date(line)
                        if header_date:
                            record_entry(current_entry)
                            current_entry = {'date': header_date, 'script': None, 'duration': None, 'plugins': set()}
                        
                        script = parse_script(line)
                        if script:
                            current_entry['script'] = script
                            if '/vendor/' in script or '/app/code/' in script or '/app/design/' in script:
                                categorize_path(script)
                        
                        duration = parse_duration(line)
                        if duration is not None:
                            current_entry['duration'] = duration
                        
                        trace_func, trace_path = parse_trace_details(line)
                        if not trace_func:
                            trace_func = parse_trace_function(line)
                        if not trace_path:
                            trace_path = parse_trace_path(line)
                        
                        if trace_func:
                            function_counts[trace_func] += 1
                        
                        if trace_path:
                            categorize_path(trace_path)
                            plugin = extract_plugin(trace_path)
                            if plugin:
                                current_entry['plugins'].add(plugin)
                                plugin_function_counts[plugin][trace_func or 'unknown'] += 1
                    
                    record_entry(current_entry)
                    f.close()
                except Exception as e:
                    print(f"{Colors.YELLOW}Error reading {os.path.basename(log_file)}: {e}{Colors.RESET}")
            
            if not slow_requests:
                print(f"{Colors.GREEN}No slow requests found in the specified period{Colors.RESET}")
                return {}
            
            # Calculate averages and sort
            slow_scripts = []
            for script, data in slow_requests.items():
                timed_count = data['timed_count']
                avg_time = data['total_time'] / timed_count if timed_count > 0 else None
                slow_scripts.append({
                    'script': script,
                    'count': data['count'],
                    'avg_time': round(avg_time, 3) if avg_time is not None else None,
                    'max_time': round(data['max_time'], 3) if timed_count > 0 else None,
                    'total_time': round(data['total_time'], 3) if timed_count > 0 else None,
                    'timed_count': timed_count
                })
            
            # Sort by total time when available, otherwise by count
            slow_scripts.sort(
                key=lambda x: (x['total_time'] if x['total_time'] is not None else 0, x['count']),
                reverse=True
            )
            
            result = {
                'period_days': days,
                'total_slow_requests': sum(s['count'] for s in slow_scripts),
                'timed_slow_requests': sum(s['timed_count'] for s in slow_scripts),
                'unique_scripts': len(slow_scripts),
                'top_slow_scripts': slow_scripts[:top_n],
                'trace_plugins': [],
                'trace_themes': [],
                'trace_functions': [],
                'trace_sources': dict(source_counts),
                'trace_summary': {
                    'unique_plugins': len(plugin_trace_hits),
                    'unique_themes': len(theme_counts),
                    'unique_functions': len(function_counts)
                },
                'plugin_breakdown': []
            }
            
            if plugin_trace_hits:
                result['trace_plugins'] = [
                    {'plugin': plugin, 'count': count}
                    for plugin, count in sorted(plugin_trace_hits.items(), key=lambda x: x[1], reverse=True)[:10]
                ]
            if theme_counts:
                result['trace_themes'] = [
                    {'theme': theme, 'count': count}
                    for theme, count in sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:5]
                ]
            if function_counts:
                result['trace_functions'] = [
                    {'function': func, 'count': count}
                    for func, count in sorted(function_counts.items(), key=lambda x: x[1], reverse=True)[:10]
                ]
            
            if plugin_trace_hits:
                plugin_summary = []
                for plugin, trace_hits in sorted(plugin_trace_hits.items(), key=lambda x: x[1], reverse=True):
                    entry_count = plugin_entry_counts.get(plugin, 0)
                    functions = plugin_function_counts.get(plugin, {})
                    top_functions = [
                        {'function': func, 'count': count}
                        for func, count in sorted(functions.items(), key=lambda x: x[1], reverse=True)[:5]
                    ]
                    plugin_summary.append({
                        'plugin': plugin,
                        'entry_count': entry_count,
                        'trace_hits': trace_hits,
                        'top_functions': top_functions
                    })
                result['plugin_breakdown'] = plugin_summary[:10]
            
            access_summary = self._load_access_log_timing(days)
            if access_summary:
                script_index = access_summary.get('script_index', {})
                
                def access_match(script_path: str) -> Optional[Dict]:
                    if not script_path:
                        return None
                    clean = script_path.split('?')[0]
                    keys = {clean}
                    if clean.startswith('/'):
                        keys.add(clean.lstrip('/'))
                    else:
                        keys.add('/' + clean)
                    keys.add(os.path.basename(clean))
                    for key in keys:
                        if key in script_index:
                            return script_index[key]
                    return None
                
                for script_data in slow_scripts:
                    match = access_match(script_data['script'])
                    if match:
                        script_data['access_avg_time_sec'] = match['avg_time_sec']
                        script_data['access_max_time_sec'] = match['max_time_sec']
                        script_data['access_count'] = match['count']
                
                if 'script_index' in access_summary:
                    del access_summary['script_index']
                result['access_log_correlation'] = access_summary
            
            print(f"\n{Colors.RED}Top {top_n} Slowest Scripts:{Colors.RESET}")
            print(f"{'Script':<50} {'Count':<8} {'Avg Time':<10} {'Max Time':<10}")
            print("=" * 80)
            
            for script_data in result['top_slow_scripts']:
                script_name = os.path.basename(script_data['script'])
                avg_time = script_data['avg_time']
                max_time = script_data['max_time']
                avg_display = f"{avg_time:.3f}s" if avg_time is not None else "n/a"
                max_display = f"{max_time:.3f}s" if max_time is not None else "n/a"
                
                if avg_time is None:
                    color = Colors.ORANGE
                else:
                    color = Colors.RED if avg_time > 5 else Colors.ORANGE if avg_time > 2 else Colors.GREEN
                
                print(f"{color}{script_name:<50} {script_data['count']:<8} {avg_display:<10} {max_display:<10}{Colors.RESET}")
            
            missing_duration = result['total_slow_requests'] - result['timed_slow_requests']
            if result['total_slow_requests'] > 0 and missing_duration > 0:
                missing_percent = round((missing_duration / result['total_slow_requests']) * 100, 2)
                print(f"\n{Colors.ORANGE}Missing duration on {missing_duration} entries ({missing_percent}%){Colors.RESET}")
                result['anomalies'] = {
                    'missing_duration_count': missing_duration,
                    'missing_duration_percent': missing_percent
                }
            
            if result['trace_plugins']:
                print(f"\n{Colors.CYAN}Top Modules in Slow Traces:{Colors.RESET}")
                for item in result['trace_plugins']:
                    print(f"  {item['plugin']}: {item['count']} hits")
            
            if result['plugin_breakdown']:
                print(f"\n{Colors.CYAN}Top Module Functions (by trace hits):{Colors.RESET}")
                for plugin_entry in result['plugin_breakdown'][:5]:
                    print(
                        f"  {plugin_entry['plugin']}: "
                        f"{plugin_entry['entry_count']} entries, "
                        f"{plugin_entry['trace_hits']} trace hits"
                    )
                    for func in plugin_entry.get('top_functions', []):
                        print(f"    - {func['function']}(): {func['count']} hits")
            
            if result['trace_functions']:
                print(f"\n{Colors.CYAN}Top Functions in Slow Traces:{Colors.RESET}")
                for item in result['trace_functions']:
                    print(f"  {item['function']}(): {item['count']} hits")
            
            if access_summary:
                print(f"\n{Colors.CYAN}Access Log Timing for Slow Scripts:{Colors.RESET}")
                for script_data in result['top_slow_scripts']:
                    avg_time = script_data.get('access_avg_time_sec')
                    max_time = script_data.get('access_max_time_sec')
                    count = script_data.get('access_count')
                    if avg_time is None or max_time is None or count is None:
                        continue
                    script_name = os.path.basename(script_data['script'])
                    print(
                        f"  {script_name:<30} "
                        f"Avg {avg_time:.3f}s | "
                        f"Max {max_time:.3f}s | "
                        f"Count {count}"
                    )
            
            return result
            
        except Exception as e:
            print(f"{Colors.RED}Error analyzing slow logs: {e}{Colors.RESET}")
            return {}

    def _load_access_log_timing(self, days: int = 7) -> Dict:
        """Load access log timings to correlate with slow log scripts"""
        try:
            access_patterns = [
                f"{self.log_path}/php-app.access.log*",
                f"{self.log_path}/php*.access.log*"
            ]
            
            log_files = []
            for pattern in access_patterns:
                log_files.extend(glob.glob(pattern))
            
            log_files = list(set(log_files))
            if not log_files:
                return {}
            
            cutoff_date = datetime.now() - timedelta(days=days)
            access_parser = ResourceAnalyzer(self.site_url, log_path=self.log_path)
            
            script_stats = {}
            
            def normalize_script(script: str) -> str:
                if not script:
                    return ''
                return script.split('?')[0]
            
            for log_file in log_files:
                try:
                    try:
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
                        if file_mtime < cutoff_date - timedelta(days=1):
                            continue
                    except Exception:
                        pass
                    
                    if log_file.endswith('.gz'):
                        import gzip
                        f = gzip.open(log_file, 'rt', errors='ignore')
                    else:
                        f = open(log_file, 'r', errors='ignore')
                    
                    for line in f:
                        log_date = access_parser._parse_log_datetime(line)
                        if log_date and log_date < cutoff_date:
                            continue
                        
                        metrics = access_parser._extract_access_metrics(line)
                        if not metrics:
                            continue
                        
                        req_time = metrics.get('request_time_sec')
                        script = normalize_script(metrics.get('script') or '')
                        if not script or req_time is None or req_time <= 0:
                            continue
                        
                        entry = script_stats.setdefault(
                            script, {'count': 0, 'total_time': 0.0, 'max_time': 0.0}
                        )
                        entry['count'] += 1
                        entry['total_time'] += req_time
                        entry['max_time'] = max(entry['max_time'], req_time)
                    
                    f.close()
                except Exception:
                    continue
            
            if not script_stats:
                return {}
            
            def script_keys(script: str) -> List[str]:
                keys = set()
                clean = normalize_script(script)
                if not clean:
                    return []
                keys.add(clean)
                if clean.startswith('/'):
                    keys.add(clean.lstrip('/'))
                else:
                    keys.add('/' + clean)
                keys.add(os.path.basename(clean))
                return list(keys)
            
            script_index = {}
            for script, stats in script_stats.items():
                avg_time = stats['total_time'] / stats['count'] if stats['count'] > 0 else 0
                summary = {
                    'script': script,
                    'count': stats['count'],
                    'avg_time_sec': round(avg_time, 3),
                    'max_time_sec': round(stats['max_time'], 3)
                }
                for key in script_keys(script):
                    script_index[key] = summary
            
            sorted_scripts = sorted(
                script_stats.items(),
                key=lambda x: x[1]['total_time'],
                reverse=True
            )
            
            scripts_summary = []
            for script, stats in sorted_scripts:
                avg_time = stats['total_time'] / stats['count'] if stats['count'] > 0 else 0
                scripts_summary.append({
                    'script': script,
                    'count': stats['count'],
                    'avg_time_sec': round(avg_time, 3),
                    'max_time_sec': round(stats['max_time'], 3)
                })
            
            return {
                'scripts': scripts_summary,
                'script_index': script_index
            }
        
        except Exception:
            return {}



class ResourceAnalyzer(MagentoHealthMonitor):
    """Analyze memory and CPU usage from PHP access logs"""
    
    def __init__(self, site_url: str, magento_root: str = None, log_path: str = None):
        super().__init__(site_url, magento_root)
        self.log_path = log_path or "../logs"
    
    def _parse_log_datetime(self, line: str) -> Optional[datetime]:
        patterns = [
            (re.compile(r'\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2})'), '%d/%b/%Y:%H:%M:%S'),
            (re.compile(r'\[(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2})\]'), '%d-%b-%Y %H:%M:%S'),
            (re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]'), '%Y-%m-%d %H:%M:%S'),
            (re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'), '%Y-%m-%d %H:%M:%S'),
        ]
        
        for regex, fmt in patterns:
            match = regex.search(line)
            if match:
                try:
                    return datetime.strptime(match.group(1), fmt)
                except Exception:
                    continue
        return None
    
    def _normalize_time_seconds(self, value: float, unit: Optional[str]) -> Optional[float]:
        if value <= 0:
            return None
        if unit:
            unit = unit.lower()
            if unit in ('ms', 'msec'):
                return value / 1000
            return value
        if value > 1000:
            return value / 1000
        return value
    
    def _normalize_memory_mb(self, value: float, unit: Optional[str]) -> Optional[float]:
        if value <= 0:
            return None
        if unit:
            unit = unit.lower()
            if unit in ('b', 'bytes'):
                return value / (1024 * 1024)
            if unit in ('kb', 'k'):
                return value / 1024
            if unit in ('mb', 'm'):
                return value
            if unit in ('gb', 'g'):
                return value * 1024
        if value >= 1024 * 1024:
            return value / (1024 * 1024)
        if value >= 5000:
            return value / 1024
        return value
    
    def _extract_script_from_line(self, line: str) -> Optional[str]:
        match = re.search(r'(?:script_filename|script)\s*=\s*(\S+)', line, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip('"').strip("'")
        
        request_path = self._extract_request_path(line)
        if request_path and '.php' in request_path:
            return request_path.split('?')[0]
        
        request_match = re.search(
            r'"(?:GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH)\s+([^" ]+)',
            line,
            re.IGNORECASE
        )
        if request_match:
            request_path = request_match.group(1)
            if '.php' in request_path:
                return request_path.split('?')[0]
        
        php_matches = re.findall(r'(\S+\.php(?:\?\S+)?)', line)
        if php_matches:
            return php_matches[-1].split('?')[0]
        
        return None

    def _extract_request_path(self, line: str) -> Optional[str]:
        request_match = re.search(
            r'"(?:GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH)\s+([^" ]+)',
            line,
            re.IGNORECASE
        )
        if request_match:
            return request_match.group(1)
        return None
    
    def _extract_access_metrics(self, line: str) -> Dict[str, Optional[float]]:
        metrics = {
            'request_time_sec': None,
            'memory_mb': None,
            'cpu_percent': None,
            'script': self._extract_script_from_line(line)
        }

        # Cloudways-style logs often have two quoted strings (request + path)
        parts = line.split('"')
        if len(parts) >= 4:
            request_part = parts[1].strip()
            after_request = parts[2]
            trailing_path = parts[3].strip()

            if request_part:
                req_tokens = request_part.split()
                if len(req_tokens) >= 2:
                    req_path = req_tokens[1]
                    if '.php' in req_path:
                        metrics['script'] = req_path.split('?')[0]

            if metrics['script'] is None and trailing_path and '.php' in trailing_path:
                metrics['script'] = trailing_path.split('?')[0]

            tokens = [t for t in after_request.split() if t and t != '-']
            if tokens and re.fullmatch(r'\d{3}', tokens[0]):
                tokens = tokens[1:]

            percent_tokens = [t for t in tokens if t.endswith('%')]
            numeric_tokens = []
            for t in tokens:
                if t.endswith('%'):
                    continue
                if re.fullmatch(r'-?\d+(?:\.\d+)?', t):
                    numeric_tokens.append(float(t))

            if percent_tokens and metrics['cpu_percent'] is None:
                for p in percent_tokens:
                    try:
                        metrics['cpu_percent'] = float(p.strip('%'))
                        break
                    except Exception:
                        continue

            if numeric_tokens:
                if metrics['request_time_sec'] is None:
                    candidates = [v for v in numeric_tokens if 0 < v <= 60]
                    if candidates:
                        metrics['request_time_sec'] = min(candidates)

                if metrics['memory_mb'] is None:
                    largest = max(numeric_tokens)
                    if largest > 100:
                        metrics['memory_mb'] = self._normalize_memory_mb(largest, None)
        
        time_match = re.search(
            r'(?:req(?:uest)?_?time|duration|elapsed|time)[:=]\s*(\d+(?:\.\d+)?)\s*(ms|msec|s|sec|seconds)?',
            line,
            re.IGNORECASE
        )
        if time_match:
            metrics['request_time_sec'] = self._normalize_time_seconds(
                float(time_match.group(1)),
                time_match.group(2)
            )
        
        mem_match = re.search(
            r'(?:mem(?:ory)?|rss)[:=]\s*(\d+(?:\.\d+)?)\s*(kb|k|mb|m|gb|g|bytes|b)?',
            line,
            re.IGNORECASE
        )
        if mem_match:
            metrics['memory_mb'] = self._normalize_memory_mb(
                float(mem_match.group(1)),
                mem_match.group(2)
            )
        
        cpu_match = re.search(r'(?:cpu|cpu_usage)[:=]\s*(\d+(?:\.\d+)?)\s*%?', line, re.IGNORECASE)
        if cpu_match:
            try:
                metrics['cpu_percent'] = float(cpu_match.group(1))
            except Exception:
                pass
        
        if metrics['request_time_sec'] is None:
            time_unit_match = re.search(r'(\d+(?:\.\d+)?)\s*(ms|msec|s|sec)\b', line, re.IGNORECASE)
            if time_unit_match:
                metrics['request_time_sec'] = self._normalize_time_seconds(
                    float(time_unit_match.group(1)),
                    time_unit_match.group(2)
                )
        
        if metrics['memory_mb'] is None:
            mem_unit_match = re.search(r'(\d+(?:\.\d+)?)\s*(kb|k|mb|m|gb|g|bytes|b)\b', line, re.IGNORECASE)
            if mem_unit_match:
                metrics['memory_mb'] = self._normalize_memory_mb(
                    float(mem_unit_match.group(1)),
                    mem_unit_match.group(2)
                )
        
        if metrics['cpu_percent'] is None:
            cpu_percent_match = re.search(r'(\d+(?:\.\d+)?)\s*%', line)
            if cpu_percent_match:
                try:
                    metrics['cpu_percent'] = float(cpu_percent_match.group(1))
                except Exception:
                    pass
        
        if metrics['request_time_sec'] is None or metrics['memory_mb'] is None:
            after_request = line
            if '"' in line:
                after_request = line.split('"')[-1]
            tokens = [token.strip() for token in after_request.split() if token.strip()]
            
            if tokens and re.fullmatch(r'\d{3}', tokens[0]):
                tokens = tokens[1:]
            
            for idx, token in enumerate(tokens):
                if '.php' in token and metrics['script'] is None:
                    metrics['script'] = token.split('?')[0]
                    tokens.pop(idx)
                    break
            
            numeric_values = []
            for token in tokens:
                cleaned = token.strip().strip(',')
                if cleaned.endswith('%'):
                    if metrics['cpu_percent'] is None:
                        try:
                            metrics['cpu_percent'] = float(cleaned.rstrip('%'))
                        except Exception:
                            pass
                    continue
                
                unit_match = re.fullmatch(
                    r'(-?\d+(?:\.\d+)?)(ms|msec|s|sec|kb|k|mb|m|gb|g|bytes|b)',
                    cleaned,
                    re.IGNORECASE
                )
                if unit_match:
                    value = float(unit_match.group(1))
                    unit = unit_match.group(2)
                    if unit.lower() in ('ms', 'msec', 's', 'sec'):
                        if metrics['request_time_sec'] is None:
                            metrics['request_time_sec'] = self._normalize_time_seconds(value, unit)
                    else:
                        if metrics['memory_mb'] is None:
                            metrics['memory_mb'] = self._normalize_memory_mb(value, unit)
                    continue
                
                if re.fullmatch(r'-?\d+(?:\.\d+)?', cleaned):
                    numeric_values.append(float(cleaned))
            
            if numeric_values:
                candidate_time = None
                candidate_cpu = None
                for value in numeric_values:
                    if candidate_time is None and 0 < value <= 60:
                        candidate_time = value
                    elif candidate_cpu is None and 0 <= value <= 100:
                        candidate_cpu = value
                
                candidate_memory = max(numeric_values)
                
                if metrics['request_time_sec'] is None and candidate_time is not None:
                    metrics['request_time_sec'] = self._normalize_time_seconds(candidate_time, None)
                
                if metrics['memory_mb'] is None and candidate_memory is not None:
                    metrics['memory_mb'] = self._normalize_memory_mb(candidate_memory, None)
                
                if metrics['cpu_percent'] is None and candidate_cpu is not None:
                    metrics['cpu_percent'] = candidate_cpu
        
        if metrics['request_time_sec'] is None and metrics['memory_mb'] is None and metrics['cpu_percent'] is None:
            return {}
        
        return metrics

    def _percentile(self, values: List[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        sorted_values = sorted(values)
        if len(sorted_values) == 1:
            return sorted_values[0]
        rank = (percentile / 100) * (len(sorted_values) - 1)
        lower_index = int(rank)
        upper_index = min(lower_index + 1, len(sorted_values) - 1)
        fraction = rank - lower_index
        return (
            sorted_values[lower_index] * (1 - fraction) +
            sorted_values[upper_index] * fraction
        )
    
    def analyze_php_resources(self, days: int = 7) -> Dict:
        """Analyze memory and CPU usage from PHP access logs"""
        print(f"{Colors.CYAN}Analyzing PHP Resource Usage (Last {days} days)...{Colors.RESET}")
        
        try:
            # Find PHP access log files
            patterns = [
                f"{self.log_path}/php-app.access.log*",
                f"{self.log_path}/php*.access.log*"
            ]
            
            log_files = []
            for pattern in patterns:
                found = glob.glob(pattern)
                log_files.extend(found)
            
            log_files = list(set(log_files))
            
            if not log_files:
                print(f"{Colors.YELLOW}No PHP access log files found{Colors.RESET}")
                return {}
            
            print(f"Found {len(log_files)} PHP access log files")
            
            memory_usage = []
            cpu_times = []
            request_times = []
            high_memory_scripts = defaultdict(lambda: {'count': 0, 'total_memory': 0, 'max_memory': 0})
            
            cutoff_date = datetime.now() - timedelta(days=days)
            parsed_entries = 0
            
            for log_file in log_files:
                try:
                    try:
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
                        if file_mtime < cutoff_date - timedelta(days=1):
                            continue
                    except Exception:
                        pass
                    
                    if log_file.endswith('.gz'):
                        import gzip
                        f = gzip.open(log_file, 'rt', errors='ignore')
                    else:
                        f = open(log_file, 'r', errors='ignore')
                    
                    for line in f:
                        log_date = self._parse_log_datetime(line)
                        if log_date and log_date < cutoff_date:
                            continue
                        
                        metrics = self._extract_access_metrics(line)
                        if not metrics:
                            continue
                        
                        parsed_entries += 1
                        
                        req_time = metrics.get('request_time_sec')
                        memory_mb = metrics.get('memory_mb')
                        cpu_percent = metrics.get('cpu_percent')
                        script = metrics.get('script') or 'unknown'
                        
                        if req_time is not None and req_time < 300:
                            request_times.append(req_time)
                        
                        if memory_mb is not None and 0 < memory_mb < 50000:
                            memory_usage.append(memory_mb)
                            
                            if memory_mb > 100:
                                high_memory_scripts[script]['count'] += 1
                                high_memory_scripts[script]['total_memory'] += memory_mb
                                high_memory_scripts[script]['max_memory'] = max(
                                    high_memory_scripts[script]['max_memory'],
                                    memory_mb
                                )
                        
                        if cpu_percent is not None and 0 <= cpu_percent < 1000:
                            cpu_times.append(cpu_percent)
                    
                    f.close()
                except Exception as e:
                    print(f"{Colors.YELLOW}Error reading {os.path.basename(log_file)}: {e}{Colors.RESET}")
            
            result = {}
            
            if memory_usage:
                p95_mem = self._percentile(memory_usage, 95)
                result['memory'] = {
                    'average_mb': round(statistics.mean(memory_usage), 2),
                    'median_mb': round(statistics.median(memory_usage), 2),
                    'max_mb': round(max(memory_usage), 2),
                    'min_mb': round(min(memory_usage), 2),
                    'p95_mb': round(p95_mem, 2) if p95_mem is not None else None,
                    'samples': len(memory_usage)
                }
                
                avg_mem = result['memory']['average_mb']
                color = Colors.RED if avg_mem > 200 else Colors.ORANGE if avg_mem > 100 else Colors.GREEN
                print(f"\n{color}Average Memory: {avg_mem}MB | Max: {result['memory']['max_mb']}MB | P95: {result['memory']['p95_mb']}MB{Colors.RESET}")
            
            if request_times:
                p95_time = self._percentile(request_times, 95)
                result['request_time'] = {
                    'average_sec': round(statistics.mean(request_times), 3),
                    'median_sec': round(statistics.median(request_times), 3),
                    'max_sec': round(max(request_times), 3),
                    'p95_sec': round(p95_time, 3) if p95_time is not None else None,
                    'samples': len(request_times)
                }
                
                avg_time = result['request_time']['average_sec']
                color = Colors.RED if avg_time > 2 else Colors.ORANGE if avg_time > 1 else Colors.GREEN
                print(f"{color}Average Request Time: {avg_time}s | Max: {result['request_time']['max_sec']}s | P95: {result['request_time']['p95_sec']}s{Colors.RESET}")
            
            if cpu_times:
                p95_cpu = self._percentile(cpu_times, 95)
                result['cpu'] = {
                    'average_percent': round(statistics.mean(cpu_times), 2),
                    'median_percent': round(statistics.median(cpu_times), 2),
                    'max_percent': round(max(cpu_times), 2),
                    'p95_percent': round(p95_cpu, 2) if p95_cpu is not None else None,
                    'samples': len(cpu_times)
                }
                
                avg_cpu = result['cpu']['average_percent']
                color = Colors.RED if avg_cpu > 80 else Colors.ORANGE if avg_cpu > 50 else Colors.GREEN
                print(f"{color}Average CPU: {avg_cpu}% | Max: {result['cpu']['max_percent']}% | P95: {result['cpu']['p95_percent']}%{Colors.RESET}")
            
            if high_memory_scripts:
                top_memory_scripts = sorted(
                    high_memory_scripts.items(),
                    key=lambda x: x[1]['total_memory'],
                    reverse=True
                )[:5]
                
                result['high_memory_scripts'] = [
                    {
                        'script': script,
                        'count': data['count'],
                        'avg_memory_mb': round(data['total_memory'] / data['count'], 2),
                        'max_memory_mb': round(data['max_memory'], 2)
                    }
                    for script, data in top_memory_scripts
                ]
                
                print(f"\n{Colors.RED}Top 5 High Memory Scripts (>100MB):{Colors.RESET}")
                for script_data in result['high_memory_scripts']:
                    script_name = os.path.basename(script_data['script'])
                    print(f"  {script_name}: Avg {script_data['avg_memory_mb']}MB, Max {script_data['max_memory_mb']}MB ({script_data['count']} requests)")
            
            if not result:
                print(f"{Colors.YELLOW}Could not parse resource metrics from PHP access logs{Colors.RESET}")
                print(f"{Colors.YELLOW}Parsed entries: {parsed_entries}{Colors.RESET}")
            
            return result
            
        except Exception as e:
            print(f"{Colors.RED}Error analyzing PHP resources: {e}{Colors.RESET}")
            return {}



class ErrorAnalyzer(MagentoHealthMonitor):
    """Analyze HTTP errors and patterns"""
    
    def __init__(self, site_url: str, magento_root: str = None, log_path: str = None):
        super().__init__(site_url, magento_root)
        self.log_path = log_path or "../logs"
    
    def analyze_http_errors(self, days: int = 7) -> Dict:
        """Analyze HTTP error codes (404, 500, 502, 503) from access logs"""
        print(f"{Colors.CYAN}Analyzing HTTP Errors (Last {days} days)...{Colors.RESET}")
        
        error_patterns = {
            '404': defaultdict(int),
            '500': defaultdict(int),
            '502': defaultdict(int),
            '503': defaultdict(int)
        }
        error_urls = {
            '404': defaultdict(int),
            '500': defaultdict(int),
            '502': defaultdict(int),
            '503': defaultdict(int)
        }
        
        daily_errors = defaultdict(lambda: defaultdict(int))
        
        try:
            # Support wildcard patterns like *woocommerce*.access.log*
            log_files = []
            
            # Common patterns to try (exclude php-app.access.log*)
            patterns = [
                f"{self.log_path}/backend_*.access.log*",
                f"{self.log_path}/nginx-app.status.log*"
            ]
            
            for pattern in patterns:
                found_files = glob.glob(pattern)
                log_files.extend(found_files)
            
            # Remove duplicates and exclude php-app.access.log*
            log_files = [
                f for f in set(log_files)
                if not os.path.basename(f).startswith("php-app.access.log")
            ]
            
            if not log_files:
                print(f"{Colors.YELLOW}No log files found matching patterns in {self.log_path}{Colors.RESET}")
                print(f"{Colors.YELLOW}Tried patterns: backend_*.access.log*, nginx-app.status.log*{Colors.RESET}")
                return {}
            
            print(f"Found {len(log_files)} log files to analyze")
            
            cutoff_date = datetime.now() - timedelta(days=days)
            
            for log_file in log_files:
                try:
                    # Handle both plain and gzipped logs
                    if log_file.endswith('.gz'):
                        import gzip
                        f = gzip.open(log_file, 'rt')
                    else:
                        f = open(log_file, 'r')
                    
                    for line in f:
                        # Parse Apache/Nginx combined log format
                        match = re.search(r'\s(\d{3})\s', line)
                        if match:
                            status_code = match.group(1)
                            
                            # Extract date - try multiple formats
                            date_match = re.search(r'\[([^:]+)', line)
                            if date_match:
                                try:
                                    log_date = datetime.strptime(date_match.group(1), '%d/%b/%Y')
                                    if log_date >= cutoff_date:
                                        date_key = log_date.strftime('%Y-%m-%d')
                                        
                                        if status_code in error_patterns:
                                            error_patterns[status_code][date_key] += 1
                                            daily_errors[date_key][status_code] += 1
                                            
                                            request_match = re.search(
                                                r'"(?:GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH)\s+([^" ]+)',
                                                line,
                                                re.IGNORECASE
                                            )
                                            if request_match:
                                                path = request_match.group(1)
                                                error_urls[status_code][path] += 1
                                except:
                                    pass
                    
                    f.close()
                except Exception as e:
                    print(f"{Colors.YELLOW}Error reading {os.path.basename(log_file)}: {e}{Colors.RESET}")
            
            # Analyze trends
            result = {
                'period_days': days,
                'log_files_analyzed': len(log_files),
                'error_summary': {},
                'daily_breakdown': dict(daily_errors),
                'top_urls': {},
                'trends': {}
            }
            
            for error_code, dates in error_patterns.items():
                if dates:
                    total = sum(dates.values())
                    sorted_dates = sorted(dates.items())
                    
                    # Check if errors are increasing
                    if len(sorted_dates) >= 2:
                        recent_avg = statistics.mean([v for k, v in sorted_dates[-3:]])
                        older_avg = statistics.mean([v for k, v in sorted_dates[:3]])
                        trend = 'increasing' if recent_avg > older_avg * 1.2 else 'decreasing' if recent_avg < older_avg * 0.8 else 'stable'
                    else:
                        trend = 'insufficient_data'
                    
                    result['error_summary'][error_code] = {
                        'total_count': total,
                        'daily_average': round(total / days, 2),
                        'trend': trend
                    }
                    result['trends'][error_code] = trend
                    
                    status_color = Colors.RED if trend == 'increasing' else Colors.ORANGE if total > 100 else Colors.GREEN
                    print(f"{status_color}{error_code} Errors: {total} total, {round(total/days, 2)}/day avg, Trend: {trend}{Colors.RESET}")
                    
                    if error_urls.get(error_code):
                        top_urls = sorted(
                            error_urls[error_code].items(),
                            key=lambda x: x[1],
                            reverse=True
                        )[:10]
                        result['top_urls'][error_code] = [
                            {'url': url, 'count': count} for url, count in top_urls
                        ]
                        print(f"{Colors.CYAN}Top 10 URLs for {error_code}:{Colors.RESET}")
                        for url, count in top_urls:
                            print(f"  {count:<6} {url}")
            
            return result
            
        except Exception as e:
            print(f"{Colors.RED}Error analyzing logs: {e}{Colors.RESET}")
            return {}
    

class ConcurrencyEstimator(MagentoHealthMonitor):
    """Estimate concurrent user capacity"""
    
    def estimate_concurrent_users(self, test_duration: int = 30) -> Dict:
        """Estimate maximum concurrent users the site can handle"""
        print(f"{Colors.CYAN}Estimating Concurrent User Capacity...{Colors.RESET}")
        print(f"Running load test for {test_duration} seconds...")
        
        import threading
        
        max_concurrent = 0
        successful_levels = []
        
        # Test with increasing concurrency levels
        for concurrent_level in [5, 10, 20, 30, 50, 75, 100]:
            print(f"\nTesting with {concurrent_level} concurrent users...")
            
            success_count = 0
            error_count = 0
            response_times = []
            lock = threading.Lock()
            start_time = time.time()
            test_duration_per_level = 10
            
            def make_request():
                nonlocal success_count, error_count
                while time.time() - start_time < test_duration_per_level:
                    try:
                        req_start = time.time()
                        response = requests.get(self.site_url, timeout=15)
                        req_time = (time.time() - req_start) * 1000
                        
                        with lock:
                            if response.status_code == 200:
                                success_count += 1
                                response_times.append(req_time)
                            else:
                                error_count += 1
                    except:
                        with lock:
                            error_count += 1
                    time.sleep(0.2)
            
            threads = []
            for _ in range(concurrent_level):
                t = threading.Thread(target=make_request)
                t.start()
                threads.append(t)
            
            for t in threads:
                t.join()
            
            total_requests = success_count + error_count
            success_rate = (success_count / total_requests * 100) if total_requests > 0 else 0
            avg_response_time = statistics.mean(response_times) if response_times else 0
            
            print(f"  Success Rate: {success_rate:.1f}% | Avg Response: {avg_response_time:.0f}ms")
            
            # Consider successful if >95% success rate and avg response < 5 seconds
            if success_rate > 95 and avg_response_time < 5000:
                max_concurrent = concurrent_level
                successful_levels.append({
                    'concurrent_users': concurrent_level,
                    'success_rate': round(success_rate, 2),
                    'avg_response_ms': round(avg_response_time, 2)
                })
            else:
                print(f"{Colors.RED}  Performance degraded at {concurrent_level} users{Colors.RESET}")
                break
            
            if time.time() - start_time > test_duration:
                break
        
        # Estimate daily capacity
        estimated_daily_users = max_concurrent * 24 * 60 * 10  # Assuming 10 page views per minute per user
        
        result = {
            'estimated_max_concurrent_users': max_concurrent,
            'estimated_daily_capacity': estimated_daily_users,
            'successful_test_levels': successful_levels,
            'recommendation': self._get_capacity_recommendation(max_concurrent)
        }
        
        status = Colors.GREEN if max_concurrent >= 50 else Colors.ORANGE if max_concurrent >= 20 else Colors.RED
        print(f"\n{status}Estimated Max Concurrent Users: {max_concurrent}{Colors.RESET}")
        print(f"Estimated Daily Capacity: ~{estimated_daily_users:,} page views")
        print(f"\nRecommendation: {result['recommendation']}")
        
        return result
    
    def _get_capacity_recommendation(self, max_concurrent: int) -> str:
        """Get recommendation based on concurrent capacity"""
        if max_concurrent >= 100:
            return "Excellent capacity. Site can handle high traffic loads."
        elif max_concurrent >= 50:
            return "Good capacity. Consider CDN and caching optimization for growth."
        elif max_concurrent >= 20:
            return "Moderate capacity. Implement caching, CDN, and consider resource upgrades."
        else:
            return "Limited capacity. Immediate optimization needed: enable caching, CDN, upgrade hosting."



class _TeeOutput:
    """Write to both original stdout and a capture buffer simultaneously"""
    def __init__(self, original, capture):
        self.original = original
        self.capture = capture

    def write(self, data):
        self.original.write(data)
        self.capture.write(data)

    def flush(self):
        self.original.flush()
        self.capture.flush()

    def isatty(self):
        return self.original.isatty()


class HealthReportGenerator:
    """Generate comprehensive Magento health report"""

    def __init__(self, site_url: str, log_path: str = None, output_path: str = None, magento_root: str = None):
        self.site_url = site_url.rstrip("/")
        self.log_path = log_path
        self.magento_root = magento_root
        # Default: current working directory (where you ran the script)
        self.output_path = os.path.abspath(output_path or os.getcwd())
        self.report = {
            'site_url': site_url,
            'timestamp': datetime.now().isoformat(),
            'magento': {},
            'frontend': {},
            'backend': {},
            'cache': {},
            'indexers_cron': {},
            'modules_search': {},
            'resources': {},
            'slow_logs': {},
            'errors': {},
            'capacity': {}
        }

    def generate_full_report(self):
        """Generate complete health report"""
        import sys, io
        self._log_capture = io.StringIO()
        self._tee = _TeeOutput(sys.stdout, self._log_capture)
        sys.stdout = self._tee

        print(f"{Colors.BOLD}{Colors.CYAN}")
        print("=" * 70)
        print("MAGENTO 2 COMPREHENSIVE HEALTH REPORT")
        print("=" * 70)
        print(f"{Colors.RESET}")
        print(f"Site: {self.site_url}")
        print(f"Output directory: {self.output_path}")
        print(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        root = self.magento_root

        # Magento installation
        backend = MagentoBackendMetrics(self.site_url, root)
        backend.print_section("MAGENTO INSTALLATION")
        self.report['magento'] = backend.check_installation()

        # Frontend Metrics
        frontend = FrontendMetrics(self.site_url, root)
        frontend.print_section("FRONTEND PERFORMANCE METRICS")

        self.report['frontend']['ttfb'] = frontend.measure_ttfb()
        self.report['frontend']['page_load'] = frontend.measure_fcp_and_page_load()
        self.report['frontend']['page_size'] = frontend.measure_page_size()
        self.report['frontend']['throughput'] = frontend.measure_throughput()

        found = self.report['magento'].get('found')
        mods = MagentoModulesSearch(self.site_url, root)
        if found:
            # Backend Metrics
            backend.print_section("BACKEND & DATABASE METRICS")

            self.report['backend']['database'] = backend.check_database_size()
            self.report['backend']['query_performance'] = backend.check_database_query_performance()
            self.report['backend']['cleanup'] = backend.check_table_bloat()
            self.report['backend']['memory'] = backend.check_memory_usage()

            # Cache & FPC
            cache = MagentoCacheMetrics(self.site_url, root)
            cache.print_section("CACHE & FULL PAGE CACHE")

            self.report['cache']['types'] = cache.check_cache_status()
            self.report['cache']['full_page'] = cache.check_full_page_cache()
            self.report['cache']['backends'] = cache.check_cache_backends()

            # Indexers & Cron
            idx = MagentoIndexerCron(self.site_url, root)
            idx.print_section("INDEXERS, CRON & QUEUES")

            self.report['indexers_cron']['indexers'] = idx.check_indexers()
            self.report['indexers_cron']['cron'] = idx.check_cron()
            self.report['indexers_cron']['queues'] = idx.check_message_queues()

            # Modules & Search
            mods.print_section("MODULES & SEARCH ENGINE")

            self.report['modules_search']['modules'] = mods.check_modules()
            self.report['modules_search']['search'] = mods.check_search_engine()

        else:
            print(f"{Colors.YELLOW}Skipping Magento backend, cache, indexer/cron and module checks (Magento root not found){Colors.RESET}")

        # PHP Resource Analysis
        resources = ResourceAnalyzer(self.site_url, log_path=self.log_path)
        resources.print_section("PHP RESOURCE ANALYSIS (Memory & CPU)")

        self.report['resources'] = resources.analyze_php_resources(days=7)

        # Slow Log Analysis
        slow_logs = SlowLogAnalyzer(self.site_url, log_path=self.log_path)
        slow_logs.print_section("SLOW LOG ANALYSIS")

        self.report['slow_logs'] = slow_logs.analyze_slow_logs(days=7, top_n=10)

        # Error Analysis
        errors = ErrorAnalyzer(self.site_url, log_path=self.log_path)
        errors.print_section("ERROR ANALYSIS & PATTERNS")

        self.report['errors']['http_errors'] = errors.analyze_http_errors(days=7)
        if found:
            self.report['errors']['magento_logs'] = mods.check_application_logs()

        # Capacity Estimation
        capacity = ConcurrencyEstimator(self.site_url, root)
        capacity.print_section("CONCURRENT USER CAPACITY ESTIMATION")

        self.report['capacity'] = capacity.estimate_concurrent_users(test_duration=30)

        # Generate Summary
        self._print_summary()

        # Save report
        self._save_json_report()

        return self.report

    def _print_summary(self):
        """Print executive summary"""
        print(f"\n{Colors.BOLD}{Colors.CYAN}")
        print("=" * 70)
        print("EXECUTIVE SUMMARY")
        print("=" * 70)
        print(f"{Colors.RESET}")

        issues = []
        warnings = []
        r = self.report
        ic = r['indexers_cron']
        ms = r['modules_search']

        # Check for critical issues
        if not r['magento'].get('found'):
            issues.append("Magento root not found - app-level checks skipped")
        if r['frontend'].get('ttfb', {}).get('status') == 'critical':
            issues.append("Critical TTFB (>1000ms)")
        if r['frontend'].get('page_load', {}).get('page_load_status') == 'critical':
            issues.append("Slow page load (>5s)")
        if r['capacity'].get('estimated_max_concurrent_users', 0) < 20:
            issues.append("Low concurrent user capacity (<20)")
        if r['cache'].get('types', {}).get('disabled'):
            issues.append(f"Disabled cache types: {', '.join(r['cache']['types']['disabled'])}")
        if ic.get('indexers', {}).get('invalid'):
            issues.append(f"Indexers need reindex: {', '.join(ic['indexers']['invalid'])}")
        if ic.get('cron') and not ic['cron'].get('cron_running'):
            issues.append("Cron not running (no successful job in 15 min)")
        if ms.get('search', {}).get('status') == 'critical':
            issues.append("Search engine unreachable or cluster red")

        # Check for warnings
        if r['magento'].get('deploy_mode') not in (None, 'production'):
            warnings.append(f"Deploy mode is {r['magento'].get('deploy_mode')} (use production)")
        if r['frontend'].get('page_size', {}).get('size_status') == 'warning':
            warnings.append("Large page size (>2MB)")
        if r['cache'].get('full_page', {}).get('status') == 'warning':
            warnings.append("Homepage not consistently served from full page cache")
        if r['cache'].get('full_page', {}).get('application') == 'Built-in':
            warnings.append("Built-in FPC in use (Varnish recommended)")
        if r['cache'].get('backends', {}).get('status') == 'warning':
            warnings.append("Cache backend is not Redis/Valkey")
        if ic.get('indexers', {}).get('update_on_save'):
            warnings.append(f"{len(ic['indexers']['update_on_save'])} indexers on 'Update on Save'")
        if ic.get('cron', {}).get('status') == 'warning':
            warnings.append("Cron errors/missed jobs or overdue backlog")
        if ic.get('queues', {}).get('status') == 'warning':
            warnings.append("Message queue backlog")
        if r['backend'].get('cleanup', {}).get('flagged'):
            warnings.append(f"Table bloat: {', '.join(r['backend']['cleanup']['flagged'])}")
        if r['backend'].get('memory', {}).get('status') == 'warning':
            warnings.append("PHP memory_limit below 756M")
        if ms.get('modules', {}).get('status') == 'warning':
            warnings.append("High third-party module count (>60)")
        if r['errors'].get('magento_logs', {}).get('status') == 'warning':
            warnings.append("Frequent CRITICAL entries in Magento logs")

        if issues:
            print(f"{Colors.RED}Critical Issues Found:{Colors.RESET}")
            for issue in issues:
                print(f"  ❌ {issue}")

        if warnings:
            print(f"\n{Colors.ORANGE}Warnings:{Colors.RESET}")
            for warning in warnings:
                print(f"  ⚠️  {warning}")

        if not issues and not warnings:
            print(f"{Colors.GREEN}✅ No critical issues detected! Site health is good.{Colors.RESET}")

        # Key metrics summary
        print(f"\n{Colors.CYAN}Key Metrics:{Colors.RESET}")
        print(f"  • Magento: {r['magento'].get('version', 'N/A')} ({r['magento'].get('deploy_mode', 'N/A')} mode)")
        print(f"  • TTFB: {r['frontend'].get('ttfb', {}).get('average_ms', 'N/A')}ms")
        print(f"  • Page Load: {r['frontend'].get('page_load', {}).get('page_load_ms', 'N/A')}ms")
        print(f"  • Throughput: {r['frontend'].get('throughput', {}).get('requests_per_second', 'N/A')} req/sec")
        print(f"  • Max Concurrent Users: {r['capacity'].get('estimated_max_concurrent_users', 'N/A')}")
        print(f"  • Database Size: {r['backend'].get('database', {}).get('total_size', 'N/A')}")
        print(f"  • FPC: {r['cache'].get('full_page', {}).get('application', 'N/A')} "
              f"(hits {r['cache'].get('full_page', {}).get('homepage_cache_hits', 'N/A')})")
        print(f"  • Third-party modules: {ms.get('modules', {}).get('third_party_count', 'N/A')}")


    def _save_json_report(self):
        """Save CLI output to plain-text report file (.txt for browser / SiteSleuth)"""
        import sys
        filename = f"magento_health_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filepath = os.path.join(self.output_path, filename)

        # Restore stdout before writing
        if hasattr(self, '_tee'):
            sys.stdout = self._tee.original

        try:
            os.makedirs(self.output_path, exist_ok=True)

            # Get captured output and strip ANSI color codes
            raw_output = self._log_capture.getvalue() if hasattr(self, '_log_capture') else ''
            ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
            clean_output = ansi_escape.sub('', raw_output)

            with open(filepath, 'w') as f:
                f.write(clean_output)

            self._ensure_txt_cors_for_sitesleuth(filepath)

            print(f"\n{Colors.GREEN}Report saved to: {filepath}{Colors.RESET}")
            download_url = self._public_download_url(filepath) or ""
            self._print_download_banner(filepath, download_url)
            return filepath
        except Exception as e:
            print(f"{Colors.RED}Error saving report: {e}{Colors.RESET}")
            return None

    def _public_download_url(self, filepath: str):
        """Build HTTPS URL from site_url + path relative to public_html."""
        try:
            from urllib.parse import urljoin
            abs_file = os.path.abspath(filepath).replace("\\", "/")
            filename = os.path.basename(abs_file)

            rel = filename
            lower = abs_file.lower()
            marker = "/public_html"
            if marker in lower:
                tail = abs_file[lower.index(marker) + len(marker):].lstrip("/")
                rel = tail if tail else filename

            return urljoin(self.site_url + "/", rel)
        except Exception:
            return None

    def _is_under_public_html(self, filepath: str) -> bool:
        return "/public_html" in os.path.abspath(filepath).replace("\\", "/").lower()

    def _print_download_banner(self, filepath: str, download_url: str):
        """Prominent end-of-run block: public URL + SiteSleuth upload hint."""
        abs_path = os.path.abspath(filepath)
        web_ok = self._is_under_public_html(filepath)
        print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 70}")
        print("DOWNLOAD LINK — SiteSleuth investigation log")
        print(f"{'=' * 70}{Colors.RESET}")
        print(f"{Colors.CYAN}Saved to (same folder you ran the script):{Colors.RESET}")
        print(f"  {abs_path}")
        if web_ok and download_url:
            print(f"\n{Colors.GREEN}Public download URL:{Colors.RESET}")
            print(f"  {download_url}")
            print(f"\n{Colors.CYAN}Quick download:{Colors.RESET}")
            print(f"  curl -fsSL \"{download_url}\" -o magento_health_report.txt")
        else:
            print(f"\n{Colors.YELLOW}Note:{Colors.RESET} File is not under public_html — no public URL.")
            print("  Run the script from the app public_html folder for a browser download link.")
        print(f"\n{Colors.YELLOW}SiteSleuth:{Colors.RESET}")
        print("  1) Download/open the .txt (public URL above, or scp the local path)")
        print("  2) SiteSleuth → case → Upload log file (.txt)")
        print("  3) Run AI investigation")
        print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 70}{Colors.RESET}\n")

    def _ensure_txt_cors_for_sitesleuth(self, report_filepath: str):
        """Allow SiteSleuth (browser fetch) to read .txt reports cross-origin."""
        try:
            out_dir = os.path.dirname(os.path.abspath(report_filepath))
            htaccess = os.path.join(out_dir, '.htaccess')
            marker = '# SiteSleuth CORS for investigation .txt reports'
            block = (
                f"\n{marker}\n"
                "<IfModule mod_headers.c>\n"
                "  <FilesMatch \"\\.(txt)$\">\n"
                "    Header set Access-Control-Allow-Origin \"*\"\n"
                "  </FilesMatch>\n"
                "</IfModule>\n"
            )
            if os.path.isfile(htaccess):
                with open(htaccess, 'r') as f:
                    existing = f.read()
                if marker in existing:
                    return
                with open(htaccess, 'a') as f:
                    f.write(block)
            else:
                with open(htaccess, 'w') as f:
                    f.write(block.lstrip('\n'))
            print(f"{Colors.GREEN}CORS enabled for .txt in {htaccess} (SiteSleuth browser fetch){Colors.RESET}")
        except Exception as e:
            print(f"{Colors.YELLOW}Note: could not update .htaccess for CORS ({e}){Colors.RESET}")



def main():
    """Main execution function"""
    import sys
    import argparse

    parser = argparse.ArgumentParser(
        description='Magento 2 Comprehensive Health Monitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  cd /home/master/applications/APP/public_html
  curl -s https://raw.githubusercontent.com/jahanzaibakhan/python/main/magento-test.py | python3 - \\
    https://example.com --log-path ../logs

  # Magento root elsewhere / custom output folder:
  %(prog)s https://example.com --magento-root /var/www/magento --output-path /tmp/reports
        '''
    )

    parser.add_argument('site_url', help='Magento store URL (e.g., https://example.com)')
    parser.add_argument('--log-path', '-l', default='../logs',
                       help='Path to web server / PHP-FPM log files. Default: ../logs')
    parser.add_argument('--magento-root', '-m', default=None,
                       help='Magento root (contains bin/magento). Default: current directory or its parent')
    parser.add_argument('--output-path', '-o', default=None,
                       help='Directory for the .txt report. Default: current working directory (where you run the script)')

    args = parser.parse_args()

    global _MAGENTO_ROOT_OVERRIDE
    _MAGENTO_ROOT_OVERRIDE = os.path.abspath(args.magento_root) if args.magento_root else None

    output_path = os.path.abspath(args.output_path) if args.output_path else os.getcwd()
    reporter = HealthReportGenerator(args.site_url, log_path=args.log_path, output_path=output_path,
                                     magento_root=_MAGENTO_ROOT_OVERRIDE)
    reporter.generate_full_report()


if __name__ == "__main__":
    main()
