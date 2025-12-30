# Note: Run the following in terminal to login to Internet Archive...
# ia configure --username="your_email_here" --password="your_password_here"

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import hashlib
import os
import time
import json
from datetime import datetime, timedelta
from pathlib import Path
import logging
import shutil
import tempfile


class WebsiteArchiver:
    def __init__(self, base_url, output_dir="snapshots", enable_internet_archive=False, ia_collection="opensource"):
        self.base_url = base_url.rstrip('/')
        self.domain = urlparse(base_url).netloc
        self.output_dir = Path(output_dir)
        self.assets_dir = self.output_dir / "assets"
        self.snapshots_dir = self.output_dir / self.domain
        self.enable_internet_archive = enable_internet_archive
        self.ia_collection = ia_collection

        # Create directory structure
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Setup logging
        logging.basicConfig(
            level=logging.DEBUG,  # Changed to DEBUG to see comparison details
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(self.output_dir / 'archiver.log', encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

        # Track downloaded assets
        self.asset_cache = self._load_asset_cache()

        # Check for internetarchive library if enabled
        if self.enable_internet_archive:
            try:
                import internetarchive
                self.ia = internetarchive
                self.logger.info("Internet Archive integration enabled")
            except ImportError:
                self.logger.error("internetarchive library not installed. Run: pip install internetarchive")
                self.enable_internet_archive = False

    def _load_asset_cache(self):
        """Load existing asset cache to avoid re-downloading"""
        cache_file = self.assets_dir / "asset_cache.json"
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
        return {}

    def _save_asset_cache(self):
        """Save asset cache"""
        cache_file = self.assets_dir / "asset_cache.json"
        with open(cache_file, 'w') as f:
            json.dump(self.asset_cache, f, indent=2)

    def _get_file_hash(self, content):
        """Generate hash for content deduplication"""
        return hashlib.sha256(content).hexdigest()[:16]

    def _download_asset(self, url):
        """Download an asset (image, CSS, JS, etc.)"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            }
            response = requests.get(url, timeout=10, headers=headers)
            response.raise_for_status()
            return response.content
        except Exception as e:
            self.logger.warning(f"Failed to download {url}: {e}")
            return None

    def _save_asset(self, url, content):
        """Save asset with deduplication"""
        content_hash = self._get_file_hash(content)

        # Check if we already have this exact file
        if content_hash in self.asset_cache:
            self.logger.info(f"Asset already cached: {url}")
            return self.asset_cache[content_hash]

        # Determine file extension
        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1] or '.bin'

        # Determine asset type directory
        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico']:
            asset_type = 'images'
        elif ext in ['.css']:
            asset_type = 'css'
        elif ext in ['.js']:
            asset_type = 'js'
        elif ext in ['.woff', '.woff2', '.ttf', '.eot']:
            asset_type = 'fonts'
        else:
            asset_type = 'other'

        # Create subdirectory
        asset_dir = self.assets_dir / asset_type
        asset_dir.mkdir(exist_ok=True)

        # Save with hash-based filename
        filename = f"{content_hash}{ext}"
        filepath = asset_dir / filename

        with open(filepath, 'wb') as f:
            f.write(content)

        # Update cache
        relative_path = f"assets/{asset_type}/{filename}"
        self.asset_cache[content_hash] = relative_path
        self._save_asset_cache()

        self.logger.info(f"Saved new asset: {relative_path}")
        return relative_path

    def _rewrite_html(self, html, page_url, snapshot_dir):
        """Rewrite HTML to use local assets"""
        soup = BeautifulSoup(html, 'html.parser')
        assets_used = []

        # Process images
        for tag in soup.find_all('img'):
            if tag.get('src'):
                asset_url = urljoin(page_url, tag['src'])
                content = self._download_asset(asset_url)
                if content:
                    local_path = self._save_asset(asset_url, content)
                    # Calculate relative path from snapshot to assets
                    tag['src'] = f"../../{local_path}"
                    assets_used.append(local_path)

        # Process CSS links
        for tag in soup.find_all('link', rel='stylesheet'):
            if tag.get('href'):
                asset_url = urljoin(page_url, tag['href'])
                content = self._download_asset(asset_url)
                if content:
                    local_path = self._save_asset(asset_url, content)
                    tag['href'] = f"../../{local_path}"
                    assets_used.append(local_path)

        # Process inline styles with URLs
        for tag in soup.find_all(style=True):
            style = tag['style']
            # Simple URL extraction from CSS (could be improved)
            if 'url(' in style:
                self.logger.warning(f"Inline style with URL found, may need manual handling")

        # Process scripts
        for tag in soup.find_all('script', src=True):
            asset_url = urljoin(page_url, tag['src'])
            content = self._download_asset(asset_url)
            if content:
                local_path = self._save_asset(asset_url, content)
                tag['src'] = f"../../{local_path}"
                assets_used.append(local_path)

        return str(soup), assets_used

    def _archive_page(self, url, snapshot_dir):
        """Archive a single page"""
        try:
            self.logger.info(f"Archiving: {url}")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            }
            response = requests.get(url, timeout=15, headers=headers)
            response.raise_for_status()

            # Rewrite HTML and download assets
            rewritten_html, assets_used = self._rewrite_html(
                response.text, url, snapshot_dir
            )

            # Generate filename from URL path
            parsed = urlparse(url)
            path = parsed.path.strip('/') or 'index'
            if not path.endswith('.html'):
                path = path.replace('/', '_') + '.html'

            # Save HTML
            html_file = snapshot_dir / path
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(rewritten_html)

            return {
                'url': url,
                'file': str(path),
                'assets': assets_used,
                'status': 'success'
            }

        except Exception as e:
            self.logger.error(f"Failed to archive {url}: {e}")
            return {
                'url': url,
                'status': 'failed',
                'error': str(e)
            }

    def snapshot(self, pages_to_archive=None):
        """Create a complete snapshot of the website"""
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

        self.logger.info(f"Starting snapshot check: {timestamp}")

        # Default to just the homepage if no pages specified
        if pages_to_archive is None:
            pages_to_archive = [self.base_url]

        # First, download and check for changes WITHOUT saving yet
        temp_pages = []
        for page_url in pages_to_archive:
            # Ensure full URL
            if not page_url.startswith('http'):
                page_url = urljoin(self.base_url, page_url)

            try:
                self.logger.info(f"Checking: {page_url}")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
                }
                response = requests.get(page_url, timeout=15, headers=headers)
                response.raise_for_status()

                # Store temporarily for comparison
                temp_pages.append({
                    'url': page_url,
                    'html': response.text,
                    'status': 'success'
                })
            except Exception as e:
                self.logger.error(f"Failed to fetch {page_url}: {e}")
                temp_pages.append({
                    'url': page_url,
                    'status': 'failed',
                    'error': str(e)
                })

        # Check if content has changed compared to previous snapshot
        if not self._has_content_changed_from_temp(temp_pages):
            self.logger.info("=" * 60)
            self.logger.info("NO CHANGES DETECTED - Skipping snapshot creation")
            self.logger.info("=" * 60)
            return None

        # Content has changed, proceed with full snapshot
        self.logger.info("=" * 60)
        self.logger.info("CHANGES DETECTED - Creating new snapshot")
        self.logger.info("=" * 60)

        snapshot_dir = self.snapshots_dir / timestamp
        snapshot_dir.mkdir(exist_ok=True)

        # Now process and save the pages we already fetched
        manifest = {
            'timestamp': timestamp,
            'base_url': self.base_url,
            'pages': []
        }

        for temp_page in temp_pages:
            if temp_page['status'] == 'success':
                # Rewrite HTML and download assets
                rewritten_html, assets_used = self._rewrite_html(
                    temp_page['html'], temp_page['url'], snapshot_dir
                )

                # Generate filename from URL path
                parsed = urlparse(temp_page['url'])
                path = parsed.path.strip('/') or 'index'
                if not path.endswith('.html'):
                    path = path.replace('/', '_') + '.html'

                # Save rewritten HTML
                html_file = snapshot_dir / path
                with open(html_file, 'w', encoding='utf-8') as f:
                    f.write(rewritten_html)

                # Also save original HTML for comparison purposes
                original_file = snapshot_dir / (path.replace('.html', '_original.html'))
                with open(original_file, 'w', encoding='utf-8') as f:
                    f.write(temp_page['html'])

                manifest['pages'].append({
                    'url': temp_page['url'],
                    'file': str(path),
                    'original_file': str(original_file.name),
                    'assets': assets_used,
                    'status': 'success'
                })
            else:
                manifest['pages'].append({
                    'url': temp_page['url'],
                    'status': 'failed',
                    'error': temp_page.get('error', 'Unknown error')
                })

        # Save manifest
        manifest_file = snapshot_dir / 'manifest.json'
        with open(manifest_file, 'w') as f:
            json.dump(manifest, f, indent=2)

        # Generate change summary
        change_summary = self._generate_change_summary(snapshot_dir, temp_pages)

        self.logger.info(f"Snapshot complete: {timestamp}")
        self.logger.info(f"Archived {len(manifest['pages'])} pages")
        self.logger.info(f"Total unique assets: {len(self.asset_cache)}")

        # Upload to Internet Archive if enabled
        if self.enable_internet_archive:
            self._upload_to_internet_archive(snapshot_dir, manifest)

        return snapshot_dir

    def _create_bundled_snapshot(self, snapshot_dir, manifest):
        """Create a self-contained bundle with all assets for distribution on Internet Archive"""
        self.logger.info("Creating bundled snapshot for Internet Archive...")

        # Create temporary directory for bundled version
        bundle_dir = tempfile.mkdtemp(prefix="snapshot_bundle_")
        bundle_path = Path(bundle_dir)

        # Copy all HTML files
        for page in manifest['pages']:
            if page['status'] == 'success':
                src_file = snapshot_dir / page['file']
                dst_file = bundle_path / page['file']
                dst_file.parent.mkdir(parents=True, exist_ok=True)

                # Read HTML and rewrite asset paths for bundled structure
                with open(src_file, 'r', encoding='utf-8') as f:
                    html_content = f.read()

                # Replace ../../assets/ with ./assets/
                html_content = html_content.replace('../../assets/', './assets/')

                with open(dst_file, 'w', encoding='utf-8') as f:
                    f.write(html_content)

        # Collect all unique assets used in this snapshot
        assets_used = set()
        for page in manifest['pages']:
            if page['status'] == 'success' and 'assets' in page:
                assets_used.update(page['assets'])

        # Copy assets to bundle
        for asset_path in assets_used:
            src_asset = self.output_dir / asset_path
            dst_asset = bundle_path / asset_path

            if src_asset.exists():
                dst_asset.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_asset, dst_asset)

        # Copy manifest
        shutil.copy2(snapshot_dir / 'manifest.json', bundle_path / 'manifest.json')

        # Copy CHANGES.txt if it exists
        changes_file = snapshot_dir / 'CHANGES.txt'
        if changes_file.exists():
            shutil.copy2(changes_file, bundle_path / 'CHANGES.txt')

        # Add README
        readme_content = f"""# Website Archive: {self.domain}
Snapshot taken: {manifest['timestamp']}
Source: {self.base_url}

## How to view:
1. Extract this archive
2. Open any .html file in your web browser
3. All assets are included in the assets/ folder

## Contents:
- {len(manifest['pages'])} pages archived
- {len(assets_used)} unique assets included
- CHANGES.txt - Summary of what changed since last snapshot

## About this archive:
This archive was created for accountability and research purposes.
Each snapshot is self-contained with all necessary assets to view the pages
exactly as they appeared at the time of archiving.

See CHANGES.txt for details on what changed compared to the previous snapshot.
"""
        with open(bundle_path / 'README.md', 'w') as f:
            f.write(readme_content)

        self.logger.info(f"Bundle created with {len(assets_used)} assets")
        return bundle_path

    def _normalize_html_for_comparison(self, html):
        """Normalize HTML by removing dynamic elements that don't represent real changes"""
        from bs4 import BeautifulSoup
        import re

        # First pass through BeautifulSoup to normalize tag formatting
        soup = BeautifulSoup(html, 'html.parser')

        # Remove tracking parameters from URLs
        for tag in soup.find_all(['a', 'link', 'script', 'img'], href=True):
            href = tag.get('href', '')
            # Remove common tracking parameters
            href = re.sub(r'\?_gl=[^"\'>\s]+', '', href)
            href = re.sub(r'&_gl=[^"\'>\s]+', '', href)
            href = re.sub(r'\?_ga=[^"\'>\s]+', '', href)
            href = re.sub(r'&_ga=[^"\'>\s]+', '', href)
            tag['href'] = href

        for tag in soup.find_all(['img', 'script'], src=True):
            src = tag.get('src', '')
            src = re.sub(r'\?_gl=[^"\'>\s]+', '', src)
            src = re.sub(r'&_gl=[^"\'>\s]+', '', src)
            src = re.sub(r'\?_ga=[^"\'>\s]+', '', src)
            src = re.sub(r'&_ga=[^"\'>\s]+', '', src)
            tag['src'] = src

        # Convert back to string (this normalizes all tag formatting)
        text = str(soup)

        # Remove Cloudflare challenge parameters (change on every request)
        text = re.sub(r"window\.__CF\$cv\$params=\{[^}]+\}", "window.__CF$cv$params={[REMOVED]}", text)
        text = re.sub(r"r:'[a-f0-9]+'", "r:'[REMOVED]'", text)
        text = re.sub(r"t:'[A-Za-z0-9+/=]+'", "t:'[REMOVED]'", text)

        # Remove relative time indicators (like "5 days ago", "19 h.")
        # These update constantly but don't represent content changes
        text = re.sub(r'\d+\s*(h\.|hours?|mins?|minutes?|days?|weeks?|months?)\s*(ago)?\.?', '[TIME]', text,
                      flags=re.IGNORECASE)

        # Remove Facebook pixel tracking
        text = re.sub(r'<img[^>]*facebook\.com/tr[^>]*>', '', text)

        # Normalize whitespace
        text = ' '.join(text.split())

        return text

    def _has_content_changed_from_temp(self, temp_pages):
        """Check if content has changed by comparing temp downloads to previous snapshot"""
        previous_snapshot = self._get_most_recent_snapshot()

        if not previous_snapshot:
            self.logger.info("No previous snapshot found - treating as changed")
            return True

        self.logger.info(f"Comparing to previous snapshot: {previous_snapshot.name}")

        # Load previous manifest
        previous_manifest_file = previous_snapshot / 'manifest.json'
        if not previous_manifest_file.exists():
            self.logger.info("Previous manifest not found - treating as changed")
            return True

        with open(previous_manifest_file, 'r') as f:
            previous_manifest = json.load(f)

        # Compare number of pages
        if len(temp_pages) != len(previous_manifest['pages']):
            self.logger.info("Different number of pages - content changed")
            return True

        # Compare HTML content for each page
        for temp_page in temp_pages:
            if temp_page['status'] != 'success':
                continue

            # Find corresponding page in previous snapshot
            prev_page = None
            for p in previous_manifest['pages']:
                if p['url'] == temp_page['url'] and p['status'] == 'success':
                    prev_page = p
                    break

            if not prev_page:
                self.logger.info(f"New page detected: {temp_page['url']}")
                return True

            # Load previous HTML (use original if available, otherwise rewritten)
            if 'original_file' in prev_page:
                prev_html_file = previous_snapshot / prev_page['original_file']
            else:
                # Fallback for older snapshots without original files
                prev_html_file = previous_snapshot / prev_page['file']

            if not prev_html_file.exists():
                self.logger.info(f"Previous file not found: {prev_page['file']}")
                return True

            with open(prev_html_file, 'r', encoding='utf-8') as f:
                prev_html = f.read()

            # Normalize both versions for comparison
            curr_normalized = self._normalize_html_for_comparison(temp_page['html'])
            prev_normalized = self._normalize_html_for_comparison(prev_html)

            if curr_normalized != prev_normalized:
                self.logger.info(f"Changes detected in: {temp_page['url']}")

                # Save normalized versions for manual inspection
                debug_dir = self.output_dir / "debug_comparison"
                debug_dir.mkdir(exist_ok=True)

                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                curr_file = debug_dir / f"current_{timestamp}.txt"
                prev_file = debug_dir / f"previous_{timestamp}.txt"

                with open(curr_file, 'w', encoding='utf-8') as f:
                    f.write(curr_normalized)
                with open(prev_file, 'w', encoding='utf-8') as f:
                    f.write(prev_normalized)

                self.logger.info(f"Saved normalized HTML for comparison:")
                self.logger.info(f"  Current: {curr_file}")
                self.logger.info(f"  Previous: {prev_file}")
                self.logger.info("  Use a diff tool to see exact differences")

                # Show basic stats
                self.logger.info(f"Length difference: {len(curr_normalized)} vs {len(prev_normalized)}")

                # Find first difference
                min_len = min(len(curr_normalized), len(prev_normalized))
                for i in range(min_len):
                    if curr_normalized[i] != prev_normalized[i]:
                        start = max(0, i - 150)
                        end = min(len(curr_normalized), i + 150)
                        self.logger.info(f"First difference at position {i}:")
                        self.logger.info(f"Current:  ...{curr_normalized[start:end]}...")
                        self.logger.info(f"Previous: ...{prev_normalized[start:end]}...")
                        break

                return True

        self.logger.info("No content changes detected")
        return False

    def _generate_change_summary(self, snapshot_dir, temp_pages):
        """Generate a human-readable summary of what changed"""
        previous_snapshot = self._get_most_recent_snapshot()

        summary_lines = []
        summary_lines.append("=" * 70)
        summary_lines.append(f"WEBSITE CHANGE SUMMARY")
        summary_lines.append(f"Snapshot Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        summary_lines.append(f"Website: {self.base_url}")
        summary_lines.append("=" * 70)
        summary_lines.append("")

        if not previous_snapshot:
            summary_lines.append("INITIAL SNAPSHOT - No previous version to compare")
            summary_lines.append(f"Archived {len(temp_pages)} pages")
        else:
            summary_lines.append(f"Compared to: {previous_snapshot.name}")
            summary_lines.append("")

            # Load previous manifest
            previous_manifest_file = previous_snapshot / 'manifest.json'
            if previous_manifest_file.exists():
                with open(previous_manifest_file, 'r') as f:
                    previous_manifest = json.load(f)

                changes_found = False

                # Check each page for changes
                for temp_page in temp_pages:
                    if temp_page['status'] != 'success':
                        continue

                    # Find corresponding previous page
                    prev_page = None
                    for p in previous_manifest['pages']:
                        if p['url'] == temp_page['url'] and p['status'] == 'success':
                            prev_page = p
                            break

                    if not prev_page:
                        summary_lines.append(f"NEW PAGE ADDED:")
                        summary_lines.append(f"  URL: {temp_page['url']}")
                        summary_lines.append("")
                        changes_found = True
                        continue

                    # Load previous HTML (use original if available)
                    if 'original_file' in prev_page:
                        prev_html_file = previous_snapshot / prev_page['original_file']
                    else:
                        prev_html_file = previous_snapshot / prev_page['file']

                    if not prev_html_file.exists():
                        continue

                    with open(prev_html_file, 'r', encoding='utf-8') as f:
                        prev_html = f.read()

                    # Compare content
                    curr_normalized = self._normalize_html_for_comparison(temp_page['html'])
                    prev_normalized = self._normalize_html_for_comparison(prev_html)

                    if curr_normalized != prev_normalized:
                        summary_lines.append(f"CHANGES DETECTED:")
                        summary_lines.append(f"  Page: {temp_page['url']}")

                        # Calculate rough percentage of change
                        len_diff = abs(len(curr_normalized) - len(prev_normalized))
                        avg_len = (len(curr_normalized) + len(prev_normalized)) / 2
                        percent_change = (len_diff / avg_len) * 100 if avg_len > 0 else 0

                        summary_lines.append(
                            f"  Content length: {len(prev_normalized):,} → {len(curr_normalized):,} chars")
                        summary_lines.append(f"  Approximate change: {percent_change:.1f}%")

                        # Try to identify type of change
                        if len(curr_normalized) > len(prev_normalized) * 1.1:
                            summary_lines.append(f"  Type: Significant content addition")
                        elif len(curr_normalized) < len(prev_normalized) * 0.9:
                            summary_lines.append(f"  Type: Significant content removal")
                        else:
                            summary_lines.append(f"  Type: Content modification")

                        summary_lines.append("")
                        changes_found = True

                if not changes_found:
                    summary_lines.append("NO CHANGES DETECTED")
                    summary_lines.append("All monitored pages remain identical to previous snapshot")
            else:
                summary_lines.append("Previous snapshot incomplete - treating as changed")

        summary_lines.append("")
        summary_lines.append("=" * 70)
        summary_lines.append(f"Pages monitored: {len(temp_pages)}")
        summary_lines.append(f"Snapshot stored at: {snapshot_dir}")
        summary_lines.append("=" * 70)

        # Save to file
        summary_file = snapshot_dir / 'CHANGES.txt'
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(summary_lines))

        self.logger.info(f"Change summary saved to: {summary_file}")

        return '\n'.join(summary_lines)

    def _get_most_recent_snapshot(self):
        """Get the path to the most recent snapshot"""
        snapshots = sorted([d for d in self.snapshots_dir.iterdir() if d.is_dir()])
        if snapshots:
            return snapshots[-1]
        return None

    def _get_previous_snapshot(self):
        """Get the path to the most recent previous snapshot"""
        snapshots = sorted([d for d in self.snapshots_dir.iterdir() if d.is_dir()])
        if len(snapshots) >= 2:
            return snapshots[-2]  # Second to last (last is current)
        return None

    def _has_content_changed(self, current_snapshot_dir):
        """Check if content has changed compared to previous snapshot"""
        previous_snapshot = self._get_previous_snapshot()

        if not previous_snapshot:
            self.logger.info("No previous snapshot found - treating as changed")
            return True

        self.logger.info(f"Comparing to previous snapshot: {previous_snapshot.name}")

        # Load both manifests
        current_manifest_file = current_snapshot_dir / 'manifest.json'
        previous_manifest_file = previous_snapshot / 'manifest.json'

        if not previous_manifest_file.exists():
            self.logger.info("Previous manifest not found - treating as changed")
            return True

        with open(current_manifest_file, 'r') as f:
            current_manifest = json.load(f)
        with open(previous_manifest_file, 'r') as f:
            previous_manifest = json.load(f)

        # Compare number of pages
        if len(current_manifest['pages']) != len(previous_manifest['pages']):
            self.logger.info("Different number of pages - content changed")
            return True

        # Compare HTML content for each page
        changes_detected = False
        for curr_page, prev_page in zip(current_manifest['pages'], previous_manifest['pages']):
            if curr_page['status'] != 'success' or prev_page['status'] != 'success':
                continue

            curr_html = current_snapshot_dir / curr_page['file']
            prev_html = previous_snapshot / prev_page['file']

            if not prev_html.exists():
                self.logger.info(f"New page detected: {curr_page['file']}")
                changes_detected = True
                continue

            # Compare file contents
            with open(curr_html, 'r', encoding='utf-8') as f:
                curr_content = f.read()
            with open(prev_html, 'r', encoding='utf-8') as f:
                prev_content = f.read()

            if curr_content != prev_content:
                self.logger.info(f"Changes detected in: {curr_page['file']}")
                changes_detected = True

        if not changes_detected:
            self.logger.info("No content changes detected")

        return changes_detected

    def _upload_to_internet_archive(self, snapshot_dir, manifest):
        """Upload snapshot to Internet Archive (only called when changes detected)"""
        try:
            self.logger.info("Uploading to Internet Archive...")

            # Create bundled version
            bundle_path = self._create_bundled_snapshot(snapshot_dir, manifest)

            # Create unique identifier for this snapshot
            timestamp = manifest['timestamp'].replace(':', '-').replace(' ', '_')
            item_id = f"{self.domain.replace('.', '-')}-snapshot-{timestamp}"

            self.logger.info(f"Uploading to Internet Archive as: {item_id}")

            # Prepare metadata (all values must be strings)
            metadata = {
                'title': f'{self.domain} Website Snapshot - {manifest["timestamp"]}',
                'mediatype': 'web',
                'collection': self.ia_collection,
                'description': f'Automated archive of {self.base_url} for accountability and research purposes. Snapshot taken on {manifest["timestamp"]}.',
                'subject': ['web archive', 'accountability', self.domain, 'website snapshot'],
                'date': manifest['timestamp'],
                'creator': 'Automated Website Archiver',
                'source_url': self.base_url,
                'pages_archived': str(len(manifest['pages']))
            }

            # Create a zip file of the bundle
            zip_filename = f"{item_id}.zip"
            zip_path = self.output_dir / zip_filename
            shutil.make_archive(
                str(zip_path.with_suffix('')),
                'zip',
                bundle_path
            )

            # Upload to Internet Archive
            self.logger.info("Uploading to Internet Archive (this may take a few minutes)...")
            result = self.ia.upload(
                item_id,
                files=[str(zip_path)],
                metadata=metadata,
                retries=3,
                checksum=True
            )

            # Clean up
            shutil.rmtree(bundle_path)
            zip_path.unlink()

            ia_url = f"https://archive.org/details/{item_id}"
            self.logger.info(f"[SUCCESS] Successfully uploaded to: {ia_url}")

            # Save IA URL to manifest
            manifest['internet_archive_url'] = ia_url
            manifest_file = snapshot_dir / 'manifest.json'
            with open(manifest_file, 'w') as f:
                json.dump(manifest, f, indent=2)

        except Exception as e:
            self.logger.error(f"Failed to upload to Internet Archive: {e}")
            self.logger.info("Snapshot saved locally but not uploaded")

    def _wait_until_next_interval(self, interval_hours=4):
        """Wait until the next clean interval (e.g., 12am, 4am, 8am, etc.)"""
        now = datetime.now()
        current_hour = now.hour

        # Calculate next interval hour
        next_hour = ((current_hour // interval_hours) + 1) * interval_hours
        if next_hour >= 24:
            # Next interval is tomorrow
            next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            # Next interval is today
            next_run = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)

        wait_seconds = (next_run - now).total_seconds()

        self.logger.info(f"Next snapshot scheduled for: {next_run.strftime('%Y-%m-%d %I:%M %p')}")
        self.logger.info(f"Waiting {wait_seconds / 3600:.2f} hours...")

        time.sleep(wait_seconds)

    def run_continuous(self, pages_to_archive, interval_hours=4):
        """Run archiver continuously at specified intervals"""
        self.logger.info(f"Starting continuous archiving every {interval_hours} hours")
        self.logger.info("Snapshots will occur at clean intervals (e.g., 12am, 4am, 8am, 12pm, 4pm, 8pm)")
        self.logger.info("Snapshots only created when content changes are detected")

        while True:
            try:
                # Wait until next clean interval
                self._wait_until_next_interval(interval_hours)

                # Check and snapshot (only saves if changed)
                result = self.snapshot(pages_to_archive)

                if result is None:
                    self.logger.info("Next check scheduled for next interval")

            except KeyboardInterrupt:
                self.logger.info("Archiver stopped by user")
                break
            except Exception as e:
                self.logger.error(f"Error in continuous run: {e}")
                self.logger.info("Waiting 5 minutes before retry...")
                time.sleep(300)

#------------------------------------USER VARIABLES BELOW---------------------------------------
# USER Example usage
if __name__ == "__main__":
    # Configure your archiving
    archiver = WebsiteArchiver(
        base_url="https://example.com",
        output_dir="snapshots",
        enable_internet_archive=False,  # Set to False for testing, True for production
        ia_collection="opensource"  # IA collection (use "test_collection" for testing)
    )

    # USER Define pages to archive (add more as needed)
    pages = [
        "https://example.com/",
        "https://example.com/about"
        # Add more pages here
    ]

    # Run once for testing
    print("Running single snapshot...")
    archiver.snapshot(pages)

    # To run continuously (uncomment next line):
    # archiver.run_continuous(pages, interval_hours=4) #USER set interval as required, be wary of rate-limiting
