#!/usr/bin/env python3
"""
WordPress/WooCommerce Comprehensive Health Monitor
Author: Advanced Performance Monitoring Script
Description: Full CLI output with human-readable .log file generation.
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
from datetime import datetime
import requests

# Color codes for terminal output
class Colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

class HealthReportGenerator:
    def __init__(self, site_url, log_path="../logs", output_path="../private_html"):
        self.site_url = site_url.rstrip('/')
        self.log_path = log_path
        self.output_path = output_path
        self.report_buffer = [] # Buffer to store CLI output for the log file
        self.is_root = subprocess.run(['id', '-u'], capture_output=True, text=True).stdout.strip() == '0'
        self.wp_cli = f"wp {'--allow-root' if self.is_root else ''} --skip-themes --skip-plugins"

    def log(self, message, color=None):
        """Print to CLI and buffer for the log file."""
        plain_message = re.sub(r'\033\[[0-9;]*m', '', message) # Remove colors for file
        self.report_buffer.append(plain_message)
        if color:
            print(f"{color}{message}{Colors.RESET}")
        else:
            print(message)

    def run_check(self):
        self.log("="*70)
        self.log("WORDPRESS/WOOCOMMERCE COMPREHENSIVE HEALTH REPORT")
        self.log("="*70 + "\n")
        self.log(f"Site: {self.site_url}")
        self.log(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        # --- FRONTEND SECTION ---
        self.log("="*60, Colors.BOLD)
        self.log("FRONTEND PERFORMANCE METRICS", Colors.BOLD)
        self.log("="*60)
        
        self.log("Measuring TTFB (Time to First Byte)...")
        # Simulating logic from your snippet
        avg_ttfb = 341.0
        self.log(f"Average TTFB: {avg_ttfb}ms")
        self.log("Threshold: <600ms (Good), <1000ms (Warning), >=1000ms (Critical)\n")

        self.log("Measuring FCP and Page Load Time...")
        self.log("Page Load Time: 360.04ms")
        self.log("Estimated FCP: 252.03ms\n")

        # --- BACKEND SECTION ---
        self.log("="*60, Colors.BOLD)
        self.log("BACKEND & DATABASE METRICS", Colors.BOLD)
        self.log("="*60)
        
        self.log("Checking Database Size...")
        self.log("Database Size: 22 MB")
        self.log("\nTop 10 Largest Tables:")
        tables = [("wp_posts", "18.20"), ("wp_options", "2.08"), ("wp_postmeta", "0.28")]
        for name, size in tables:
            self.log(f"  {name}\t{size}")

        self.log("\nChecking Autoloaded Options...")
        self.log("Autoload Size: 203 KB (0.2 MB)")
        
        self.log("\nTesting Database Query Performance...")
        self.log("Core Queries")
        self.log("Published Posts Count: 149.51ms")
        self.log("  EXPLAIN: 1 SIMPLE wp_posts index NULL type_status_author 172 NULL 339 Using where; Using index")

        # --- CAPACITY SECTION ---
        self.log("\n" + "="*60, Colors.BOLD)
        self.log("CONCURRENT USER CAPACITY ESTIMATION", Colors.BOLD)
        self.log("="*60)
        self.log("Estimating Concurrent User Capacity...")
        self.log("Testing with 10 concurrent users...")
        self.log("  Success Rate: 100.0% | Avg Response: 3854ms")
        self.log("\nEstimated Max Concurrent Users: 10")
        self.log("Recommendation: Limited capacity. Optimization needed.")

        # --- SUMMARY & SAVING ---
        self.log("\n" + "="*70)
        self.log("EXECUTIVE SUMMARY")
        self.log("="*70)
        self.log("Critical Issues Found: \u274c Low concurrent user capacity (<20)")
        self.log(f"Key Metrics: TTFB: {avg_ttfb}ms | Load Time: 360ms")
        
        self._save_to_log()

    def _save_to_log(self):
        """Save the captured CLI output to a .log file."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"wp_health_report_{timestamp}.log"
        filepath = os.path.join(self.output_path, filename)

        try:
            if not os.path.exists(self.output_path):
                os.makedirs(self.output_path)
            
            with open(filepath, 'w') as f:
                f.write("\n".join(self.report_buffer))
            
            self.log(f"\n{Colors.GREEN}Report saved to: {filepath}{Colors.RESET}")
        except Exception as e:
            print(f"Error saving log: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('site_url')
    parser.add_argument('--log-path', default='../logs')
    parser.add_argument('--output-path', default='../private_html')
    args = parser.parse_args()

    # In your real script, you would integrate this logic into the WordPressHealthMonitor class
    reporter = HealthReportGenerator(args.site_url, args.log_path, args.output_path)
    reporter.run_check()
