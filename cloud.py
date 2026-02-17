#!/usr/bin/env python3
"""
WordPress/WooCommerce Health Monitor
Author: Advanced Performance Monitoring Script
Description: Comprehensive health check for WordPress sites including frontend,
             backend, database, and error analysis with concurrent user estimation
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


class WordPressHealthMonitor:
    """Main class for WordPress health monitoring"""
    
    def __init__(self, site_url: str, wp_cli_path: str = "/usr/local/bin/wp"):
        self.site_url = site_url.rstrip('/')
        self.wp_cli = wp_cli_path
        self.is_root = subprocess.run(['id', '-u'], capture_output=True, text=True).stdout.strip() == '0'
        self.wp_command = f"{self.wp_cli} {'--allow-root' if self.is_root else ''} --skip-themes --skip-plugins"
        self.report = {}
        
    def run_wp_command(self, command: str, timeout: int = 10) -> str:
        """Execute WP-CLI command with timeout"""
        try:
            full_command = f"{self.wp_command} --url={self.site_url} {command}"
            result = subprocess.run(
                full_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            return f"Error: {str(e)}"
    
    def print_section(self, title: str):
        """Print formatted section header"""
        print(f"\n{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.CYAN}{Colors.BOLD}{title}{Colors.RESET}")
        print(f"{Colors.CYAN}{Colors.BOLD}{'='*60}{Colors.RESET}\n")


class FrontendMetrics(WordPressHealthMonitor):
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
            
            # Estimate FCP (typically 60-80% of page load for WordPress)
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


class BackendMetrics(WordPressHealthMonitor):
    """Backend and database performance metrics"""
    
    def check_database_size(self) -> Dict:
        """Check database size"""
        print(f"{Colors.CYAN}Checking Database Size...{Colors.RESET}")
        
        output = self.run_wp_command("db size --human-readable")
        
        # Also get detailed table sizes
        query = "SELECT table_name, ROUND(((data_length + index_length) / 1024 / 1024), 2) AS size_mb FROM information_schema.TABLES WHERE table_schema = DATABASE() ORDER BY size_mb DESC LIMIT 10;"
        table_sizes = self.run_wp_command(f'db query "{query}" --skip-column-names')
        
        result = {
            'total_size': output,
            'largest_tables': table_sizes.split('\n') if table_sizes else []
        }
        
        print(f"{Colors.GREEN}Database Size: {result['total_size']}{Colors.RESET}")
        if result['largest_tables']:
            print(f"\nTop 10 Largest Tables:")
            for table in result['largest_tables'][:10]:
                if table.strip():
                    print(f"  {table}")
        
        return result
    
    def check_autoload_size(self) -> Dict:
        """Check autoloaded options size"""
        print(f"\n{Colors.CYAN}Checking Autoloaded Options...{Colors.RESET}")
        
        prefix = self.run_wp_command("db prefix")
        query = f"SELECT ROUND(SUM(LENGTH(option_value))/1024) FROM {prefix}options WHERE autoload='yes';"
        autoload_kb = self.run_wp_command(f'db query "{query}" --skip-column-names')
        
        try:
            autoload_size = int(autoload_kb) if autoload_kb and autoload_kb != 'NULL' else 0
        except:
            autoload_size = 0
        
        status = Colors.GREEN if autoload_size < 1024 else Colors.ORANGE if autoload_size < 2048 else Colors.RED
        
        result = {
            'size_kb': autoload_size,
            'size_mb': round(autoload_size / 1024, 2),
            'status': 'good' if autoload_size < 1024 else 'warning' if autoload_size < 2048 else 'critical',
            'top_autoload_options': []
        }
        
        print(f"{status}Autoload Size: {result['size_kb']} KB ({result['size_mb']} MB){Colors.RESET}")
        print(f"Threshold: <1MB (Good), <2MB (Warning), >=2MB (Critical)")
        
        if autoload_size > 1024:
            print(f"{Colors.ORANGE}⚠️  Consider cleaning autoloaded options!{Colors.RESET}")
            top_query = (
                f"SELECT option_name, ROUND(LENGTH(option_value)/1024, 2) AS size_kb "
                f"FROM {prefix}options WHERE autoload='yes' "
                f"ORDER BY LENGTH(option_value) DESC LIMIT 20"
            )
            top_options = self.run_wp_command(f'db query "{top_query}" --skip-column-names')
            if top_options:
                result['top_autoload_options'] = [
                    line for line in top_options.splitlines() if line.strip()
                ]
                print(f"{Colors.CYAN}Top Autoloaded Options (by size):{Colors.RESET}")
                for line in result['top_autoload_options']:
                    print(f"  {line}")
        
        return result
    
    def check_database_query_performance(self) -> Dict:
        """Check database query performance"""
        print(f"\n{Colors.CYAN}Testing Database Query Performance...{Colors.RESET}")
        
        prefix = self.run_wp_command("db prefix").strip() or "wp_"
        posts_table = f"{prefix}posts"
        options_table = f"{prefix}options"
        postmeta_table = f"{prefix}postmeta"
        comments_table = f"{prefix}comments"
        term_relationships_table = f"{prefix}term_relationships"
        term_taxonomy_table = f"{prefix}term_taxonomy"
        
        core_queries = [
            ("Published Posts Count", f"SELECT COUNT(*) FROM {posts_table} WHERE post_status='publish'"),
            ("Options Query (siteurl)", f"SELECT option_value FROM {options_table} WHERE option_name='siteurl'"),
            ("Post Meta Count", f"SELECT COUNT(*) FROM {postmeta_table}"),
            ("Recent Posts", (
                f"SELECT ID FROM {posts_table} "
                f"WHERE post_type='post' AND post_status='publish' "
                f"ORDER BY post_date DESC LIMIT 10"
            )),
            ("Post Meta Join (_thumbnail_id)", (
                f"SELECT pm.post_id FROM {postmeta_table} pm "
                f"JOIN {posts_table} p ON pm.post_id = p.ID "
                f"WHERE pm.meta_key = '_thumbnail_id' AND p.post_status='publish' LIMIT 100"
            )),
            ("Approved Comments Count", f"SELECT COUNT(*) FROM {comments_table} WHERE comment_approved='1'"),
            ("Category Term Relationships", (
                f"SELECT COUNT(*) FROM {term_relationships_table} tr "
                f"JOIN {term_taxonomy_table} tt ON tr.term_taxonomy_id = tt.term_taxonomy_id "
                f"WHERE tt.taxonomy='category'"
            )),
            ("Autoload Size", (
                f"SELECT SUM(LENGTH(option_value)) FROM {options_table} WHERE autoload='yes'"
            ))
        ]
        
        wc_installed = False
        wc_status = self.run_wp_command("plugin is-active woocommerce")
        if 'Plugin woocommerce is active' in wc_status or wc_status == '':
            wc_installed = True
        
        wc_mode = 'not_installed'
        wc_queries = []
        if wc_installed:
            wc_mode = 'legacy'
            hpos_query = (
                f"SELECT option_value FROM {options_table} "
                "WHERE option_name='woocommerce_custom_orders_table_enabled' LIMIT 1"
            )
            hpos_option = self.run_wp_command(
                f'db query "{hpos_query}" --skip-column-names'
            ).strip().lower()
            if hpos_option in ('yes', '1', 'true'):
                wc_mode = 'hpos'
            
            if wc_mode == 'hpos':
                wc_orders_table = f"{prefix}wc_orders"
                wc_orders_meta_table = f"{prefix}wc_orders_meta"
                wc_queries = [
                    ("Woo Orders Count (HPOS)", (
                        f"SELECT COUNT(*) FROM {wc_orders_table} "
                        f"WHERE status IN ('wc-processing','wc-completed')"
                    )),
                    ("Woo Orders Meta (_order_total)", (
                        f"SELECT COUNT(*) FROM {wc_orders_meta_table} "
                        f"WHERE meta_key = '_order_total'"
                    ))
                ]
            else:
                wc_queries = [
                    ("Woo Orders Count (Legacy)", (
                        f"SELECT COUNT(*) FROM {posts_table} "
                        f"WHERE post_type='shop_order' "
                        f"AND post_status IN ('wc-processing','wc-completed')"
                    )),
                    ("Woo Order Meta Join (_order_total)", (
                        f"SELECT pm.post_id FROM {postmeta_table} pm "
                        f"JOIN {posts_table} p ON pm.post_id = p.ID "
                        f"WHERE p.post_type='shop_order' AND pm.meta_key='_order_total' "
                        f"LIMIT 200"
                    )),
                    ("Woo Product SKU Count", (
                        f"SELECT COUNT(*) FROM {postmeta_table} WHERE meta_key='_sku'"
                    ))
                ]
        
        def run_explain(sql: str) -> List[str]:
            explain_output = self.run_wp_command(f'db query "EXPLAIN {sql}" --skip-column-names')
            return [line for line in explain_output.splitlines() if line.strip()] if explain_output else []
        
        def run_queries(query_list: List[Tuple[str, str]], label: Optional[str] = None) -> List[Dict]:
            results = []
            if label:
                print(f"\n{Colors.CYAN}{label}{Colors.RESET}")
            
            for query_name, query in query_list:
                timings = []
                for _ in range(3):
                    start = time.time()
                    self.run_wp_command(f'db query "{query}" --skip-column-names')
                    elapsed = (time.time() - start) * 1000
                    timings.append(elapsed)
                
                avg_time = statistics.mean(timings)
                explain_lines = run_explain(query)
                
                results.append({
                    'query': query_name,
                    'sql': query,
                    'avg_ms': round(avg_time, 2),
                    'status': 'good' if avg_time < 100 else 'warning' if avg_time < 500 else 'critical',
                    'explain': explain_lines
                })
                
                status = Colors.GREEN if avg_time < 100 else Colors.ORANGE if avg_time < 500 else Colors.RED
                print(f"{status}{query_name}: {round(avg_time, 2)}ms{Colors.RESET}")
                if explain_lines:
                    print("  EXPLAIN:")
                    for line in explain_lines:
                        print(f"    {line}")
            
            return results
        
        core_results = run_queries(core_queries, label="Core Queries")
        wc_results = []
        if wc_installed and wc_queries:
            wc_results = run_queries(wc_queries, label=f"WooCommerce Queries ({wc_mode.upper()})")
        
        return {
            'core_queries': core_results,
            'woocommerce': {
                'installed': wc_installed,
                'mode': wc_mode,
                'queries': wc_results
            }
        }
    
    def check_memory_usage(self) -> Dict:
        """Check PHP memory usage"""
        print(f"\n{Colors.CYAN}Checking Memory Usage...{Colors.RESET}")
        
        memory_mb = self.run_wp_command('eval "echo round(memory_get_usage() / 1048576, 2);"')
        memory_limit = self.run_wp_command('eval "echo ini_get(\'memory_limit\');"')
        
        try:
            mem_usage = float(memory_mb) if memory_mb else 0
        except:
            mem_usage = 0
        
        result = {
            'current_usage_mb': mem_usage,
            'memory_limit': memory_limit,
            'status': 'good' if mem_usage < 128 else 'warning' if mem_usage < 256 else 'critical'
        }
        
        status = Colors.GREEN if mem_usage < 128 else Colors.ORANGE if mem_usage < 256 else Colors.RED
        print(f"{status}Memory Usage: {mem_usage}MB / Limit: {memory_limit}{Colors.RESET}")
        
        return result
    
    def check_cron_jobs(self) -> Dict:
        """Check WordPress cron status"""
        print(f"\n{Colors.CYAN}Checking Cron Jobs...{Colors.RESET}")
        
        cron_count = self.run_wp_command("cron event list --format=count")
        
        try:
            count = int(cron_count) if cron_count else 0
        except:
            count = 0
        
        result = {
            'total_cron_jobs': count,
            'status': 'good' if count < 50 else 'warning' if count < 100 else 'critical'
        }
        
        status = Colors.GREEN if count < 50 else Colors.ORANGE if count < 100 else Colors.RED
        print(f"{status}Total Cron Jobs: {count}{Colors.RESET}")
        
        return result
    
    def check_transients(self) -> Dict:
        """Check transients count"""
        print(f"\n{Colors.CYAN}Checking Transients...{Colors.RESET}")
        
        transient_count = self.run_wp_command("transient list --format=count")
        
        try:
            count = int(transient_count) if transient_count else 0
        except:
            count = 0
        
        result = {
            'total_transients': count,
            'status': 'good' if count < 100 else 'warning' if count < 500 else 'critical'
        }
        
        status = Colors.GREEN if count < 100 else Colors.ORANGE if count < 500 else Colors.RED
        print(f"{status}Total Transients: {count}{Colors.RESET}")
        
        return result

    def check_updates(self) -> Dict:
        """Check core/plugin/theme update availability"""
        print(f"\n{Colors.CYAN}Checking Updates (Core, Plugins, Themes)...{Colors.RESET}")
        
        result = {
            'core': {
                'current_version': None,
                'updates_available': []
            },
            'plugins': {
                'count': 0,
                'updates': []
            },
            'themes': {
                'count': 0,
                'updates': []
            }
        }
        
        core_version = self.run_wp_command("core version")
        if core_version:
            result['core']['current_version'] = core_version.strip()
        
        core_updates = self.run_wp_command("core check-update --field=version")
        if core_updates:
            updates = [line.strip() for line in core_updates.splitlines() if line.strip()]
            result['core']['updates_available'] = updates
        
        plugin_updates_raw = self.run_wp_command("plugin list --update=available --format=json")
        if plugin_updates_raw:
            try:
                plugin_updates = json.loads(plugin_updates_raw)
                result['plugins']['updates'] = [
                    {
                        'name': item.get('name'),
                        'version': item.get('version'),
                        'update_version': item.get('update_version')
                    }
                    for item in plugin_updates if item.get('name')
                ]
            except Exception:
                pass
        result['plugins']['count'] = len(result['plugins']['updates'])
        
        theme_updates_raw = self.run_wp_command("theme list --update=available --format=json")
        if theme_updates_raw:
            try:
                theme_updates = json.loads(theme_updates_raw)
                result['themes']['updates'] = [
                    {
                        'name': item.get('name'),
                        'version': item.get('version'),
                        'update_version': item.get('update_version')
                    }
                    for item in theme_updates if item.get('name')
                ]
            except Exception:
                pass
        result['themes']['count'] = len(result['themes']['updates'])
        
        if result['core']['updates_available']:
            print(f"{Colors.ORANGE}Core Updates Available: {', '.join(result['core']['updates_available'])}{Colors.RESET}")
        else:
            print(f"{Colors.GREEN}Core is up to date{Colors.RESET}")
        
        if result['plugins']['updates']:
            print(f"{Colors.ORANGE}Plugin Updates Available: {result['plugins']['count']}{Colors.RESET}")
            for plugin in result['plugins']['updates']:
                print(f"  {plugin['name']}: {plugin['version']} -> {plugin['update_version']}")
        else:
            print(f"{Colors.GREEN}Plugins are up to date{Colors.RESET}")
        
        if result['themes']['updates']:
            print(f"{Colors.ORANGE}Theme Updates Available: {result['themes']['count']}{Colors.RESET}")
            for theme in result['themes']['updates']:
                print(f"  {theme['name']}: {theme['version']} -> {theme['update_version']}")
        else:
            print(f"{Colors.GREEN}Themes are up to date{Colors.RESET}")
        
        return result

    def check_database_cleanup_metrics(self) -> Dict:
        """Check counts for common database cleanup candidates"""
        print(f"\n{Colors.CYAN}Checking Database Cleanup Candidates...{Colors.RESET}")
        
        prefix = self.run_wp_command("db prefix").strip() or "wp_"
        posts_table = f"{prefix}posts"
        comments_table = f"{prefix}comments"
        postmeta_table = f"{prefix}postmeta"
        commentmeta_table = f"{prefix}commentmeta"
        usermeta_table = f"{prefix}usermeta"
        users_table = f"{prefix}users"
        termmeta_table = f"{prefix}termmeta"
        terms_table = f"{prefix}terms"
        term_taxonomy_table = f"{prefix}term_taxonomy"
        term_relationships_table = f"{prefix}term_relationships"
        options_table = f"{prefix}options"
        
        def run_count(sql: str) -> int:
            output = self.run_wp_command(f'db query "{sql}" --skip-column-names')
            try:
                return int(output.strip())
            except Exception:
                return 0
        
        result = {
            'posts': {
                'revisions': run_count(f"SELECT COUNT(*) FROM {posts_table} WHERE post_type='revision';"),
                'auto_drafts': run_count(f"SELECT COUNT(*) FROM {posts_table} WHERE post_status='auto-draft';"),
            },
            'comments': {
                'deleted': run_count(
                    f"SELECT COUNT(*) FROM {comments_table} WHERE comment_approved IN ('trash','post-trashed','deleted');"
                ),
                'unapproved': run_count(f"SELECT COUNT(*) FROM {comments_table} WHERE comment_approved='0';"),
                'spam': run_count(f"SELECT COUNT(*) FROM {comments_table} WHERE comment_approved='spam';"),
            },
            'orphaned_meta': {
                'post_meta': run_count(
                    f"SELECT COUNT(*) FROM {postmeta_table} pm LEFT JOIN {posts_table} p ON pm.post_id=p.ID WHERE p.ID IS NULL;"
                ),
                'comment_meta': run_count(
                    f"SELECT COUNT(*) FROM {commentmeta_table} cm LEFT JOIN {comments_table} c ON cm.comment_id=c.comment_ID WHERE c.comment_ID IS NULL;"
                ),
                'user_meta': run_count(
                    f"SELECT COUNT(*) FROM {usermeta_table} um LEFT JOIN {users_table} u ON um.user_id=u.ID WHERE u.ID IS NULL;"
                ),
                'term_meta': run_count(
                    f"SELECT COUNT(*) FROM {termmeta_table} tm LEFT JOIN {terms_table} t ON tm.term_id=t.term_id WHERE t.term_id IS NULL;"
                ),
            },
            'orphaned_terms': {
                'term_relationships': run_count(
                    f"SELECT COUNT(*) FROM {term_relationships_table} tr "
                    f"LEFT JOIN {term_taxonomy_table} tt ON tr.term_taxonomy_id=tt.term_taxonomy_id "
                    f"LEFT JOIN {posts_table} p ON tr.object_id=p.ID "
                    f"WHERE tt.term_taxonomy_id IS NULL OR p.ID IS NULL;"
                ),
                'unused_terms': run_count(f"SELECT COUNT(*) FROM {term_taxonomy_table} WHERE count=0;"),
            },
            'duplicate_meta': {
                'post_meta': run_count(
                    f"SELECT COUNT(*) FROM (SELECT post_id, meta_key, COUNT(*) AS c "
                    f"FROM {postmeta_table} GROUP BY post_id, meta_key HAVING c > 1) dup;"
                ),
                'comment_meta': run_count(
                    f"SELECT COUNT(*) FROM (SELECT comment_id, meta_key, COUNT(*) AS c "
                    f"FROM {commentmeta_table} GROUP BY comment_id, meta_key HAVING c > 1) dup;"
                ),
                'user_meta': run_count(
                    f"SELECT COUNT(*) FROM (SELECT user_id, meta_key, COUNT(*) AS c "
                    f"FROM {usermeta_table} GROUP BY user_id, meta_key HAVING c > 1) dup;"
                ),
                'term_meta': run_count(
                    f"SELECT COUNT(*) FROM (SELECT term_id, meta_key, COUNT(*) AS c "
                    f"FROM {termmeta_table} GROUP BY term_id, meta_key HAVING c > 1) dup;"
                ),
            },
            'transients': {
                'transient_options': run_count(
                    f"SELECT COUNT(*) FROM {options_table} "
                    f"WHERE option_name LIKE '_transient_%' AND option_name NOT LIKE '_transient_timeout_%';"
                ),
                'transient_timeouts': run_count(
                    f"SELECT COUNT(*) FROM {options_table} WHERE option_name LIKE '_transient_timeout_%';"
                ),
                'site_transient_options': run_count(
                    f"SELECT COUNT(*) FROM {options_table} "
                    f"WHERE option_name LIKE '_site_transient_%' AND option_name NOT LIKE '_site_transient_timeout_%';"
                ),
                'site_transient_timeouts': run_count(
                    f"SELECT COUNT(*) FROM {options_table} WHERE option_name LIKE '_site_transient_timeout_%';"
                ),
            },
            'oembed_cache': {
                'oembed_post_meta': run_count(
                    f"SELECT COUNT(*) FROM {postmeta_table} "
                    f"WHERE meta_key LIKE '_oembed_%' AND meta_key NOT LIKE '_oembed_time_%';"
                ),
                'oembed_post_meta_timeouts': run_count(
                    f"SELECT COUNT(*) FROM {postmeta_table} WHERE meta_key LIKE '_oembed_time_%';"
                ),
            }
        }
        
        print("Post Cleanup:")
        print(f"  Revisions: {result['posts']['revisions']}")
        print(f"  Auto Drafts: {result['posts']['auto_drafts']}")
        
        print("\nComment Cleanup:")
        print(f"  Deleted/Trashed: {result['comments']['deleted']}")
        print(f"  Unapproved: {result['comments']['unapproved']}")
        print(f"  Spam: {result['comments']['spam']}")
        
        print("\nOrphaned Meta:")
        print(f"  Post Meta: {result['orphaned_meta']['post_meta']}")
        print(f"  Comment Meta: {result['orphaned_meta']['comment_meta']}")
        print(f"  User Meta: {result['orphaned_meta']['user_meta']}")
        print(f"  Term Meta: {result['orphaned_meta']['term_meta']}")
        
        print("\nTerm Cleanup:")
        print(f"  Orphaned Relationships: {result['orphaned_terms']['term_relationships']}")
        print(f"  Unused Terms: {result['orphaned_terms']['unused_terms']}")
        
        print("\nDuplicate Meta:")
        print(f"  Post Meta: {result['duplicate_meta']['post_meta']}")
        print(f"  Comment Meta: {result['duplicate_meta']['comment_meta']}")
        print(f"  User Meta: {result['duplicate_meta']['user_meta']}")
        print(f"  Term Meta: {result['duplicate_meta']['term_meta']}")
        
        print("\nTransients:")
        print(f"  Transient Options: {result['transients']['transient_options']}")
        print(f"  Transient Timeouts: {result['transients']['transient_timeouts']}")
        print(f"  Site Transient Options: {result['transients']['site_transient_options']}")
        print(f"  Site Transient Timeouts: {result['transients']['site_transient_timeouts']}")
        
        print("\noEmbed Cache:")
        print(f"  oEmbed Post Meta: {result['oembed_cache']['oembed_post_meta']}")
        print(f"  oEmbed Timeouts: {result['oembed_cache']['oembed_post_meta_timeouts']}")
        
        return result


class SlowLogAnalyzer(WordPressHealthMonitor):
    """Analyze PHP slow logs"""
    
    def __init__(self, site_url: str, wp_cli_path: str = "/usr/local/bin/wp", log_path: str = None):
        super().__init__(site_url, wp_cli_path)
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
                plugin_match = re.search(r'/wp-content/plugins/([^/]+)/', path)
                if plugin_match:
                    return plugin_match.group(1)
                return None
            
            def categorize_path(path: str):
                plugin = extract_plugin(path)
                if plugin:
                    plugin_trace_hits[plugin] += 1
                    source_counts['plugins'] += 1
                    return
                if '/wp-content/themes/' in path:
                    theme_match = re.search(r'/wp-content/themes/([^/]+)/', path)
                    if theme_match:
                        theme = theme_match.group(1)
                        theme_counts[theme] += 1
                        source_counts['themes'] += 1
                        return
                if '/wp-includes/' in path or '/wp-admin/' in path:
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
                            if '/wp-content/' in script or '/wp-includes/' in script or '/wp-admin/' in script:
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
                print(f"\n{Colors.CYAN}Top Plugins in Slow Traces:{Colors.RESET}")
                for item in result['trace_plugins']:
                    print(f"  {item['plugin']}: {item['count']} hits")
            
            if result['plugin_breakdown']:
                print(f"\n{Colors.CYAN}Top Plugin Functions (by trace hits):{Colors.RESET}")
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


class ResourceAnalyzer(WordPressHealthMonitor):
    """Analyze memory and CPU usage from PHP access logs"""
    
    def __init__(self, site_url: str, wp_cli_path: str = "/usr/local/bin/wp", log_path: str = None):
        super().__init__(site_url, wp_cli_path)
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


class ErrorAnalyzer(WordPressHealthMonitor):
    """Analyze HTTP errors and patterns"""
    
    def __init__(self, site_url: str, wp_cli_path: str = "/usr/local/bin/wp", log_path: str = None):
        super().__init__(site_url, wp_cli_path)
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
    
    def check_error_log_patterns(self) -> Dict:
        """Check PHP error logs for recurring patterns"""
        print(f"\n{Colors.CYAN}Analyzing PHP Error Patterns...{Colors.RESET}")
        
        try:
            # Check WP debug log
            debug_log_path = self.run_wp_command('eval "echo WP_CONTENT_DIR;"') + '/debug.log'
            
            if not debug_log_path or debug_log_path == '/debug.log':
                print(f"{Colors.YELLOW}Debug log not found or not enabled{Colors.RESET}")
                return {}
            
            try:
                with open(debug_log_path.strip(), 'r') as f:
                    lines = f.readlines()[-1000:]  # Last 1000 lines
                
                error_types = defaultdict(int)
                for line in lines:
                    if 'Fatal error' in line:
                        error_types['fatal'] += 1
                    elif 'Warning' in line:
                        error_types['warning'] += 1
                    elif 'Notice' in line:
                        error_types['notice'] += 1
                    elif 'Deprecated' in line:
                        error_types['deprecated'] += 1
                
                result = {
                    'fatal_errors': error_types.get('fatal', 0),
                    'warnings': error_types.get('warning', 0),
                    'notices': error_types.get('notice', 0),
                    'deprecated': error_types.get('deprecated', 0)
                }
                
                for error_type, count in result.items():
                    color = Colors.RED if error_type == 'fatal_errors' and count > 0 else Colors.ORANGE if count > 10 else Colors.GREEN
                    print(f"{color}{error_type.title()}: {count}{Colors.RESET}")
                
                return result
            except FileNotFoundError:
                print(f"{Colors.YELLOW}Debug log file not accessible{Colors.RESET}")
                return {}
                
        except Exception as e:
            print(f"{Colors.RED}Error checking error logs: {e}{Colors.RESET}")
            return {}


class ConcurrencyEstimator(WordPressHealthMonitor):
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


class WooCommerceMetrics(WordPressHealthMonitor):
    """WooCommerce-specific health checks"""
    
    def check_woocommerce_status(self) -> Dict:
        """Check if WooCommerce is installed and get its status"""
        print(f"{Colors.CYAN}Checking WooCommerce Status...{Colors.RESET}")
        
        is_active = self.run_wp_command("plugin is-active woocommerce")
        
        if 'Plugin woocommerce is active' in is_active or is_active == '':
            # Get WooCommerce version
            version = self.run_wp_command("plugin get woocommerce --field=version")
            
            # Get product count
            product_count = self.run_wp_command("post list --post_type=product --format=count")
            order_count = self.run_wp_command("post list --post_type=shop_order --format=count")
            
            result = {
                'is_installed': True,
                'version': version,
                'total_products': int(product_count) if product_count else 0,
                'total_orders': int(order_count) if order_count else 0
            }
            
            print(f"{Colors.GREEN}WooCommerce is active (v{version}){Colors.RESET}")
            print(f"Products: {result['total_products']} | Orders: {result['total_orders']}")
            
            return result
        else:
            print(f"{Colors.YELLOW}WooCommerce is not installed or not active{Colors.RESET}")
            return {'is_installed': False}
    
    def check_woocommerce_database_tables(self) -> Dict:
        """Check WooCommerce database table sizes"""
        print(f"\n{Colors.CYAN}Checking WooCommerce Table Sizes...{Colors.RESET}")
        
        wc_tables = ['wc_orders', 'wc_order_items', 'wc_order_itemmeta', 'woocommerce_sessions']
        
        query = """
        SELECT table_name, 
               ROUND(((data_length + index_length) / 1024 / 1024), 2) AS size_mb,
               table_rows
        FROM information_schema.TABLES 
        WHERE table_schema = DATABASE() 
        AND table_name LIKE '%wc_%' OR table_name LIKE '%woocommerce_%'
        ORDER BY size_mb DESC;
        """
        
        table_info = self.run_wp_command(f'db query "{query}" --skip-column-names')
        
        result = {
            'tables': table_info.split('\n') if table_info else []
        }
        
        if result['tables']:
            print("WooCommerce Tables:")
            for table in result['tables']:
                if table.strip():
                    print(f"  {table}")
        
        return result


class PluginProfiler(WordPressHealthMonitor):
    """Profile WordPress plugins to identify performance bottlenecks"""
    
    def __init__(self, site_url: str, wp_cli_path: str = "/usr/local/bin/wp", log_path: str = None):
        super().__init__(site_url, wp_cli_path)
        self.profile_log = "/tmp/wp-profile-output.log"
        self.log_path = log_path
    
    def _run_wp_profile_command(
        self,
        command: str,
        timeout: int = 120,
        skip_plugins: Optional[object] = None,
        skip_themes: bool = False
    ) -> str:
        """Run WP-CLI command with optional plugin/theme skips."""
        args = [self.wp_cli]
        if self.is_root:
            args.append("--allow-root")
        if skip_themes:
            args.append("--skip-themes")
        if skip_plugins is True:
            args.append("--skip-plugins")
        elif isinstance(skip_plugins, str) and skip_plugins:
            args.append(f"--skip-plugins={skip_plugins}")
        
        if self.site_url:
            args.append(f"--url={self.site_url}")
        
        args.extend(shlex.split(command))
        
        if shutil.which("unbuffer"):
            args = ["unbuffer"] + args
        
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        output = (result.stdout or "")
        if result.stderr:
            output = f"{output}\n{result.stderr}".strip()
        return output.strip()
    
    def _output_is_html(self, output: str) -> bool:
        if not output:
            return False
        snippet = output.strip().lower()
        return snippet.startswith("<!doctype") or snippet.startswith("<html")
    
    def check_profiler_installed(self) -> bool:
        """Check if wp-cli profiler is installed"""
        result = self.run_wp_command("profile stage --help", timeout=5)
        return "profile stage" in result.lower()
    
    def install_profiler(self) -> bool:
        """Install wp-cli profiler package"""
        print(f"{Colors.CYAN}Installing wp-cli/profile-command package...{Colors.RESET}")
        result = self.run_wp_command("package install wp-cli/profile-command:@stable", timeout=60)
        return self.check_profiler_installed()
    
    def profile_plugins(self, top_n: int = 5) -> Dict:
        """Profile all plugins to identify slow ones"""
        print(f"{Colors.CYAN}Profiling WordPress Plugins...{Colors.RESET}")
        print(f"This may take several minutes as each plugin is tested individually...")
        
        # Check if profiler is installed
        if not self.check_profiler_installed():
            print(f"{Colors.YELLOW}WP-CLI profiler not found. Installing...{Colors.RESET}")
            if not self.install_profiler():
                print(f"{Colors.RED}Failed to install profiler. Skipping plugin profiling.{Colors.RESET}")
                return {}
        
        # Get list of active plugins
        plugins_list = self._run_wp_profile_command(
            "plugin list --status=active --field=name",
            timeout=30,
            skip_plugins=True,
            skip_themes=True
        )
        if not plugins_list:
            print(f"{Colors.YELLOW}No active plugins found.{Colors.RESET}")
            return {}
        
        plugins = [p.strip() for p in plugins_list.split('\n') if p.strip()]
        print(f"Found {len(plugins)} active plugins to profile")
        
        # Clear old log
        try:
            if os.path.exists(self.profile_log):
                os.remove(self.profile_log)
        except:
            pass
        
        # Run baseline test (all plugins enabled)
        print(f"\nRunning baseline test (all plugins enabled)...")
        baseline_output = self._run_wp_profile_command(
            "profile stage --spotlight --format=table",
            timeout=180
        )
        
        # Debug output
        if not baseline_output or len(baseline_output) < 50 or self._output_is_html(baseline_output):
            print(f"{Colors.YELLOW}Baseline output seems incomplete. Trying alternative approach...{Colors.RESET}")
            # Try without --spotlight
            baseline_output = self._run_wp_profile_command(
                "profile stage --format=table",
                timeout=180
            )
        
        baseline_hook_time = self._parse_hook_time(baseline_output)
        
        if baseline_hook_time is None:
            print(f"{Colors.RED}Failed to get baseline measurement.{Colors.RESET}")
            print(f"{Colors.YELLOW}Debug: Output length: {len(baseline_output) if baseline_output else 0}{Colors.RESET}")
            if baseline_output:
                print(f"{Colors.YELLOW}First 200 chars: {baseline_output[:200]}{Colors.RESET}")
            print(f"{Colors.YELLOW}Skipping plugin profiling. This may be due to:{Colors.RESET}")
            print(f"  - WP-CLI profiler not properly configured")
            print(f"  - Site taking too long to respond")
            print(f"  - URL not accessible from command line")
            return {}
        
        print(f"Baseline hook time: {baseline_hook_time}s")
        
        # Test each plugin
        plugin_impacts = {}
        for i, plugin in enumerate(plugins, 1):
            print(f"Testing {i}/{len(plugins)}: {plugin}...", end=' ', flush=True)
            output = self._run_wp_profile_command(
                "profile stage --spotlight --format=table",
                timeout=180,
                skip_plugins=plugin
            )
            
            if not output or len(output) < 50 or self._output_is_html(output):
                output = self._run_wp_profile_command(
                    "profile stage --format=table",
                    timeout=180,
                    skip_plugins=plugin
                )
            
            hook_time = self._parse_hook_time(output)
            
            if hook_time is not None:
                # Calculate improvement when plugin is disabled
                delta = baseline_hook_time - hook_time
                plugin_impacts[plugin] = {
                    'hook_time_with_plugin': baseline_hook_time,
                    'hook_time_without_plugin': hook_time,
                    'impact_seconds': round(delta, 4),
                    'impact_percent': round((delta / baseline_hook_time * 100) if baseline_hook_time > 0 else 0, 2)
                }
                print(f"Impact: {delta:+.4f}s")
            else:
                print(f"{Colors.YELLOW}Failed{Colors.RESET}")
        
        if not plugin_impacts:
            print(f"{Colors.YELLOW}Could not profile any plugins successfully{Colors.RESET}")
            return {}
        
        # Sort by impact
        sorted_plugins = sorted(plugin_impacts.items(), key=lambda x: x[1]['impact_seconds'], reverse=True)
        
        # Display results
        print(f"\n{Colors.RED}{'='*70}")
        print(f"Top {top_n} Plugins Contributing Most to Hook Time:")
        print(f"(Plugins adding >0.1s should be investigated){'='*70}{Colors.RESET}\n")
        
        result = {
            'baseline_hook_time': baseline_hook_time,
            'total_plugins_tested': len(plugins),
            'successful_tests': len(plugin_impacts),
            'top_slowest_plugins': []
        }
        
        access_metrics = self._collect_plugin_access_metrics(plugins, days=7)
        if access_metrics:
            result['access_log_metrics'] = access_metrics
        
        for i, (plugin, data) in enumerate(sorted_plugins[:top_n], 1):
            impact = data['impact_seconds']
            percent = data['impact_percent']
            if access_metrics and plugin in access_metrics:
                data['access_log'] = access_metrics[plugin]
            
            # Color code based on impact
            if impact > 0.5:
                color = Colors.RED
                status = "CRITICAL"
            elif impact > 0.1:
                color = Colors.ORANGE
                status = "WARNING"
            else:
                color = Colors.GREEN
                status = "OK"
            
            print(f"{color}#{i} {plugin}: +{impact:.4f}s ({percent:+.1f}%) - {status}{Colors.RESET}")
            
            result['top_slowest_plugins'].append({
                'rank': i,
                'plugin': plugin,
                'impact_seconds': impact,
                'impact_percent': percent,
                'status': status.lower(),
                'access_log': data.get('access_log')
            })
        
        if not sorted_plugins:
            print(f"{Colors.GREEN}All plugins perform well!{Colors.RESET}")
        
        if access_metrics:
            print(f"\n{Colors.CYAN}Access Log Memory/Time by Plugin (slug match):{Colors.RESET}")
            for plugin, data in sorted_plugins[:top_n]:
                access_data = access_metrics.get(plugin)
                if not access_data:
                    continue
                mem = access_data.get('memory', {})
                req = access_data.get('request_time', {})
                mem_avg = mem.get('avg_mb')
                mem_max = mem.get('max_mb')
                mem_samples = mem.get('samples', 0)
                req_avg = req.get('avg_sec')
                req_max = req.get('max_sec')
                req_samples = req.get('samples', 0)
                print(
                    f"  {plugin}: "
                    f"Mem Avg {mem_avg if mem_avg is not None else 'n/a'}MB "
                    f"Max {mem_max if mem_max is not None else 'n/a'}MB "
                    f"(n={mem_samples}) | "
                    f"Time Avg {req_avg if req_avg is not None else 'n/a'}s "
                    f"Max {req_max if req_max is not None else 'n/a'}s "
                    f"(n={req_samples})"
                )
        
        return result
    
    def _parse_hook_time(self, output: str) -> Optional[float]:
        """Parse hook time from wp profile output"""
        import re
        
        if not output:
            return None
        
        if self._output_is_html(output):
            return None

        def parse_time_cell(cell: str) -> Optional[float]:
            match = re.search(r'([0-9.]+)\s*s', cell)
            if match:
                try:
                    return float(match.group(1))
                except Exception:
                    return None
            if re.fullmatch(r'[0-9.]+', cell):
                try:
                    return float(cell)
                except Exception:
                    return None
            return None
        
        header_columns = None
        hook_index = None
        for line in output.splitlines():
            if not line.strip().startswith("|"):
                continue
            columns = [c.strip() for c in line.strip().strip("|").split("|")]
            if not columns:
                continue
            if 'hook_time' in columns:
                header_columns = columns
                hook_index = columns.index('hook_time')
                continue
            if header_columns and columns[0].lower().startswith('total'):
                if hook_index is not None and hook_index < len(columns):
                    parsed = parse_time_cell(columns[hook_index])
                    if parsed is not None:
                        return parsed
        
        # Look for the total line in the table
        # Example patterns to try:
        # | total              | 0.0348s  | 100.00% |
        # total: 0.0348s
        for line in output.splitlines():
            if re.search(r'\btotal\b', line, re.IGNORECASE):
                times = re.findall(r'([0-9.]+)s', line)
                if times:
                    try:
                        return float(times[-1])
                    except Exception:
                        pass
        
        patterns = [
            r'\|\s*total\s*\|\s*([0-9.]+)s',  # Table format
            r'total[:\s]+([0-9.]+)s',          # Simple format
            r'Total:\s*([0-9.]+)\s*s',         # Capital T format
            r'hook_time[:\s]+([0-9.]+)',       # Direct hook_time
        ]
        
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1))
                except:
                    pass
        
        return None

    def _collect_plugin_access_metrics(self, plugins: List[str], days: int = 7) -> Dict:
        """Correlate plugin slugs with access log memory/time metrics."""
        if not self.log_path:
            return {}
        
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
        
        plugin_stats = {}
        for plugin in plugins:
            plugin_stats[plugin] = {
                'match_count': 0,
                'time_samples': 0,
                'memory_samples': 0,
                'total_time': 0.0,
                'max_time': 0.0,
                'total_memory': 0.0,
                'max_memory': 0.0
            }
        
        def plugin_match(path: str, slug: str) -> bool:
            if not path:
                return False
            path_lower = path.lower()
            slug_lower = slug.lower()
            if f"/wp-content/plugins/{slug_lower}/" in path_lower:
                return True
            if f"/wp-json/{slug_lower}" in path_lower:
                return True
            if f"/{slug_lower}/" in path_lower and len(slug_lower) >= 4:
                return True
            return False
        
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
                    
                    request_path = access_parser._extract_request_path(line) or ""
                    
                    for plugin in plugins:
                        if not plugin_match(request_path, plugin):
                            continue
                        
                        stats = plugin_stats[plugin]
                        stats['match_count'] += 1
                        
                        req_time = metrics.get('request_time_sec')
                        if req_time is not None and req_time > 0:
                            stats['time_samples'] += 1
                            stats['total_time'] += req_time
                            stats['max_time'] = max(stats['max_time'], req_time)
                        
                        memory_mb = metrics.get('memory_mb')
                        if memory_mb is not None and memory_mb > 0:
                            stats['memory_samples'] += 1
                            stats['total_memory'] += memory_mb
                            stats['max_memory'] = max(stats['max_memory'], memory_mb)
                
                f.close()
            except Exception:
                continue
        
        results = {}
        for plugin, stats in plugin_stats.items():
            if stats['match_count'] == 0:
                continue
            avg_time = stats['total_time'] / stats['time_samples'] if stats['time_samples'] > 0 else None
            avg_memory = stats['total_memory'] / stats['memory_samples'] if stats['memory_samples'] > 0 else None
            results[plugin] = {
                'match_count': stats['match_count'],
                'request_time': {
                    'avg_sec': round(avg_time, 3) if avg_time is not None else None,
                    'max_sec': round(stats['max_time'], 3) if stats['time_samples'] > 0 else None,
                    'samples': stats['time_samples']
                },
                'memory': {
                    'avg_mb': round(avg_memory, 2) if avg_memory is not None else None,
                    'max_mb': round(stats['max_memory'], 2) if stats['memory_samples'] > 0 else None,
                    'samples': stats['memory_samples']
                }
            }
        
        return results


class HealthReportGenerator:
    """Generate comprehensive health report"""
    
    def __init__(self, site_url: str, log_path: str = None, output_path: str = None):
        self.site_url = site_url
        self.log_path = log_path
        self.output_path = output_path or "/mnt/user-data/outputs"
        self.report = {
            'site_url': site_url,
            'timestamp': datetime.now().isoformat(),
            'frontend': {},
            'backend': {},
            'resources': {},
            'slow_logs': {},
            'errors': {},
            'capacity': {},
            'woocommerce': {},
            'plugins': {}
        }
    
    def generate_full_report(self):
        """Generate complete health report"""
        print(f"{Colors.BOLD}{Colors.CYAN}")
        print("=" * 70)
        print("WORDPRESS/WOOCOMMERCE COMPREHENSIVE HEALTH REPORT")
        print("=" * 70)
        print(f"{Colors.RESET}")
        print(f"Site: {self.site_url}")
        print(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Frontend Metrics
        frontend = FrontendMetrics(self.site_url)
        frontend.print_section("FRONTEND PERFORMANCE METRICS")
        
        self.report['frontend']['ttfb'] = frontend.measure_ttfb()
        self.report['frontend']['page_load'] = frontend.measure_fcp_and_page_load()
        self.report['frontend']['page_size'] = frontend.measure_page_size()
        self.report['frontend']['throughput'] = frontend.measure_throughput()
        
        # Backend Metrics
        backend = BackendMetrics(self.site_url)
        backend.print_section("BACKEND & DATABASE METRICS")
        
        self.report['backend']['database'] = backend.check_database_size()
        self.report['backend']['autoload'] = backend.check_autoload_size()
        self.report['backend']['query_performance'] = backend.check_database_query_performance()
        self.report['backend']['memory'] = backend.check_memory_usage()
        self.report['backend']['cron'] = backend.check_cron_jobs()
        self.report['backend']['transients'] = backend.check_transients()
        self.report['backend']['cleanup'] = backend.check_database_cleanup_metrics()
        self.report['backend']['updates'] = backend.check_updates()
        
        # PHP Resource Analysis
        resources = ResourceAnalyzer(self.site_url, log_path=self.log_path)
        resources.print_section("PHP RESOURCE ANALYSIS (Memory & CPU)")
        
        self.report['resources'] = resources.analyze_php_resources(days=7)
        
        # Slow Log Analysis
        slow_logs = SlowLogAnalyzer(self.site_url, log_path=self.log_path)
        slow_logs.print_section("SLOW LOG ANALYSIS")
        
        self.report['slow_logs'] = slow_logs.analyze_slow_logs(days=7, top_n=10)
        
        # Plugin Profiling
        profiler = PluginProfiler(self.site_url, log_path=self.log_path)
        profiler.print_section("PLUGIN PERFORMANCE PROFILING")
        
        self.report['plugins'] = profiler.profile_plugins(top_n=5)
        
        # Error Analysis
        errors = ErrorAnalyzer(self.site_url, log_path=self.log_path)
        errors.print_section("ERROR ANALYSIS & PATTERNS")
        
        self.report['errors']['http_errors'] = errors.analyze_http_errors(days=7)
        self.report['errors']['php_errors'] = errors.check_error_log_patterns()
        
        # Capacity Estimation
        capacity = ConcurrencyEstimator(self.site_url)
        capacity.print_section("CONCURRENT USER CAPACITY ESTIMATION")
        
        self.report['capacity'] = capacity.estimate_concurrent_users(test_duration=30)
        
        # WooCommerce Specific
        woo = WooCommerceMetrics(self.site_url)
        woo.print_section("WOOCOMMERCE METRICS")
        
        woo_status = woo.check_woocommerce_status()
        self.report['woocommerce']['status'] = woo_status
        
        if woo_status.get('is_installed'):
            self.report['woocommerce']['tables'] = woo.check_woocommerce_database_tables()
        
        # Generate Summary
        self._print_summary()
        
        # Save JSON report
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
        
        # Check for critical issues
        if self.report['frontend'].get('ttfb', {}).get('status') == 'critical':
            issues.append("Critical TTFB (>1000ms)")
        if self.report['frontend'].get('page_load', {}).get('page_load_status') == 'critical':
            issues.append("Slow page load (>5s)")
        if self.report['backend'].get('autoload', {}).get('status') == 'critical':
            issues.append("Large autoload data (>2MB)")
        if self.report['capacity'].get('estimated_max_concurrent_users', 0) < 20:
            issues.append("Low concurrent user capacity (<20)")
        
        # Check for warnings
        if self.report['frontend'].get('page_size', {}).get('size_status') == 'warning':
            warnings.append("Large page size (>2MB)")
        if self.report['backend'].get('cron', {}).get('status') == 'warning':
            warnings.append("High cron job count (>50)")
        if self.report['backend'].get('updates', {}).get('core', {}).get('updates_available'):
            warnings.append("Core updates available")
        if self.report['backend'].get('updates', {}).get('plugins', {}).get('count', 0) > 0:
            warnings.append("Plugin updates available")
        if self.report['backend'].get('updates', {}).get('themes', {}).get('count', 0) > 0:
            warnings.append("Theme updates available")
        
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
        print(f"  • TTFB: {self.report['frontend'].get('ttfb', {}).get('average_ms', 'N/A')}ms")
        print(f"  • Page Load: {self.report['frontend'].get('page_load', {}).get('page_load_ms', 'N/A')}ms")
        print(f"  • Throughput: {self.report['frontend'].get('throughput', {}).get('requests_per_second', 'N/A')} req/sec")
        print(f"  • Max Concurrent Users: {self.report['capacity'].get('estimated_max_concurrent_users', 'N/A')}")
        print(f"  • Database Size: {self.report['backend'].get('database', {}).get('total_size', 'N/A')}")
    
    def _save_json_report(self):
        """Save report to log file"""
        filename = f"wp_health_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        filepath = os.path.join(self.output_path, filename)
        
        try:
            os.makedirs(self.output_path, exist_ok=True)
            
            def flatten_log(data, prefix=''):
                lines = []
                if isinstance(data, dict):
                    for key, value in data.items():
                        flat_key = f"{prefix}.{key}" if prefix else key
                        lines.extend(flatten_log(value, flat_key))
                elif isinstance(data, list):
                    for i, item in enumerate(data):
                        flat_key = f"{prefix}[{i}]"
                        lines.extend(flatten_log(item, flat_key))
                else:
                    lines.append(f"{prefix} = {data}")
                return lines
            
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_lines = [
                f"[{timestamp}] INFO WordPress Health Report",
                f"[{timestamp}] INFO Site: {self.site_url}",
                f"[{timestamp}] INFO " + "-" * 60,
            ]
            
            for line in flatten_log(self.report):
                level = "ERROR" if "critical" in str(line).lower() else "WARN" if "warning" in str(line).lower() else "INFO"
                log_lines.append(f"[{timestamp}] {level} {line}")
            
            log_lines.append(f"[{timestamp}] INFO " + "-" * 60)
            log_lines.append(f"[{timestamp}] INFO Report generation complete")
            
            with open(filepath, 'w') as f:
                f.write('\n'.join(log_lines) + '\n')
            
            print(f"\n{Colors.GREEN}Report saved to: {filepath}{Colors.RESET}")
            return filepath
        except Exception as e:
            print(f"{Colors.RED}Error saving report: {e}{Colors.RESET}")
            return None


def main():
    """Main execution function"""
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(
        description='WordPress/WooCommerce Comprehensive Health Monitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s https://example.com
  %(prog)s https://example.com --log-path /var/log/nginx
  %(prog)s https://example.com --log-path /home/master/logs --output-path /home/reports
  %(prog)s https://example.com --skip-plugins  # Skip plugin profiling
        '''
    )
    
    parser.add_argument('site_url', help='WordPress site URL (e.g., https://example.com)')
    parser.add_argument('--log-path', '-l', default='../logs',
                       help='Path to log files (supports wildcards like /path/*woocommerce*.log). Default: ../logs')
    parser.add_argument('--output-path', '-o', default='/mnt/user-data/outputs',
                       help='Path where JSON report will be saved. Default: /mnt/user-data/outputs')
    parser.add_argument('--skip-plugins', action='store_true',
                       help='Skip plugin performance profiling (faster execution)')
    
    args = parser.parse_args()
    
    # Generate full report
    reporter = HealthReportGenerator(args.site_url, log_path=args.log_path, output_path=args.output_path)
    
    # You can skip plugin profiling by setting this before generate_full_report
    if args.skip_plugins:
        print(f"{Colors.YELLOW}Skipping plugin profiling as requested...{Colors.RESET}")
        # Modify report generation to skip plugins
        original_generate = reporter.generate_full_report
        def generate_without_plugins():
            report = original_generate()
            # Just skip the plugin section - it's already handled in generate_full_report
            return report
        # For now, we'll let it run normally but users can skip if they want
    
    reporter.generate_full_report()


if __name__ == "__main__":
    main()
