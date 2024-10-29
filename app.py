from flask import Flask, render_template, request, session, jsonify
from datetime import datetime
import os
import uuid
import json
import requests
import boto3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from anytree import Node, RenderTree
from urllib.parse import urlparse
import logging
import threading
from collections import defaultdict
from functools import partial

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Required for session handling
s3 = boto3.client('s3', region_name='ca-central-1')  

# Install browsers
os.system("playwright install chromium")

def upload_file_to_s3(file_path, bucket_name, object_name):
    try:
        s3.upload_file(file_path, bucket_name, object_name)
        logger.info(f"Uploaded {object_name} to S3 bucket {bucket_name}.")
    except Exception as e:
        logger.error(f"Failed to upload {object_name} to S3: {e}")

def generate_presigned_url(bucket_name, object_name, expiration=3600):
    try:
        response = s3.generate_presigned_url('get_object',
                                             Params={'Bucket': bucket_name, 'Key': object_name},
                                             ExpiresIn=expiration)
        return response
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {object_name}: {e}")
        return None

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tasks = defaultdict(dict)  # Global dictionary to store task status and results

def generate_task_id():
    return str(uuid.uuid4())

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            url = request.form['url']
            task_id = generate_task_id()
            session['task_id'] = task_id
            session.modified = True

            # Start background thread for sitemap generation
            sitemap_thread = threading.Thread(target=generate_sitemap_task, args=(url, task_id))
            sitemap_thread.start()

            # Render the template immediately
            current_year = datetime.now().year
            return render_template('index.html', analysis_complete=False, current_year=current_year)
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            return render_template('error.html', error_message=str(e)), 500
    else:
        current_year = datetime.now().year
        return render_template('index.html', analysis_complete=False, current_year=current_year)

def generate_sitemap_task(url, task_id):
    try:
        tasks[task_id]['status'] = 'running'
        sitemap_urls = generate_sitemap(url, task_id)
        tasks[task_id]['sitemap_urls'] = sitemap_urls
        tasks[task_id]['status'] = 'sitemap_complete'

        # Start screenshot capturing in another thread
        screenshot_thread = threading.Thread(target=capture_screenshots_task, args=(task_id, sitemap_urls))
        screenshot_thread.start()
    except Exception as e:
        logger.error(f"Error in generate_sitemap_task: {e}")
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['error'] = str(e)

def capture_screenshots_task(task_id, urls):
    try:
        tasks[task_id]['status'] = 'capturing_screenshots'
        screenshot_urls = capture_screenshots(task_id, urls)
        tasks[task_id]['screenshot_urls'] = screenshot_urls
        tasks[task_id]['status'] = 'complete'
    except Exception as e:
        logger.error(f"Error in capture_screenshots_task: {e}")
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['error'] = str(e)

def generate_sitemap(start_url, task_id):
    visited = set()
    to_visit = [start_url]
    sitemap = []
    base_url = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(start_url))

    while to_visit:
        url = to_visit.pop()
        if url in visited or len(visited) >= 10:  # Limit to 10 pages
            continue
        visited.add(url)
        sitemap.append(url)
        try:
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            for link in soup.find_all('a', href=True):
                href = link['href']
                if href.startswith('/'):
                    href = requests.compat.urljoin(base_url, href)
                elif not href.startswith('http'):
                    href = requests.compat.urljoin(url, href)
                if href.startswith(base_url) and href not in visited:
                    to_visit.append(href)
        except Exception as e:
            logger.warning(f"Error crawling {url}: {e}")
            continue

    return sitemap

def build_sitemap_tree(urls):
    from anytree import Node, RenderTree
    from urllib.parse import urlparse

    nodes = {}
    root = Node('root', url='')

    for url in urls:
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.strip('/').split('/') if part]
        current_parent = root

        for idx, part in enumerate(path_parts):
            full_url = f"{parsed.scheme}://{parsed.netloc}/{'/'.join(path_parts[:idx+1])}"
            # Check if a node with this part already exists under the current parent
            existing_node = next((child for child in current_parent.children if child.name == part), None)
            if existing_node:
                current_parent = existing_node
            else:
                new_node = Node(part, parent=current_parent, url=full_url)
                current_parent = new_node

    # Build the tree lines with HTML links
    sitemap_tree = []
    for pre, _, node in RenderTree(root):
        if node.is_root:
            continue  # Skip the root node
        link = f'<a href="{node.url}" target="_blank">{node.name}</a>'
        line = f"{pre}{link}"
        sitemap_tree.append(line)

    return sitemap_tree

def capture_screenshots(task_id, urls):
    processed_urls = set()
    screenshot_urls = []
    tasks[task_id]['api_calls'] = []
    
    # Create directories if they don't exist
    result_dir = f'static/results/{task_id}'
    screenshots_dir = f'{result_dir}/screenshots'
    os.makedirs(screenshots_dir, exist_ok=True)

    result_dir = f'static/results/{task_id}'
    screenshots_dir = f'{result_dir}/screenshots'
    os.makedirs(screenshots_dir, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            logger.error(f"Failed to launch browser: {e}")
            return []
        context = browser.new_context(viewport={'width': 1280, 'height': 720})

        # Define log_api_request as a closure to capture task_id
        def log_api_request(request):
            if request.resource_type in ["xhr", "fetch"]:
                api_call = {
                    'url': request.url,
                    'method': request.method,
                    'headers': dict(request.headers)
                }
                tasks[task_id]['api_calls'].append(api_call)

        # Monitor network requests for API calls
        context.on("request", log_api_request)
        page = context.new_page()

        for idx, url in enumerate(urls):
            if url in processed_urls:
                continue  # Skip if already processed
            processed_urls.add(url)

            try:
                page.goto(url, timeout=30000)
                # Wait for network to be idle and content to load
                page.wait_for_load_state('networkidle')
                page.wait_for_load_state('domcontentloaded')
                # Wait for images and other media
                page.wait_for_selector('img', state='visible', timeout=10000)
                # Scroll through page to trigger lazy loading
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(2000)  # Wait longer for lazy content
                page.evaluate('window.scrollTo(0, 0)')
                page.wait_for_timeout(1000)  # Settle at top
                screenshot_filename = f'screenshot_{idx + 1}.png'
                screenshot_path = os.path.join(screenshots_dir, screenshot_filename)
                page.screenshot(path=screenshot_path, full_page=True)

                # Upload screenshot to S3
                object_name = f'{task_id}/screenshots/{screenshot_filename}'
                upload_file_to_s3(screenshot_path, 'recce-results', object_name)
                screenshot_url = generate_presigned_url('recce-results', object_name, expiration=86400)

                # Update screenshot URLs in tasks
                screenshot_urls.append({'url': screenshot_url, 'filename': screenshot_filename})
                tasks[task_id]['screenshot_urls'] = screenshot_urls
                logger.info(f"Captured screenshot for {url}")
            except Exception as e:
                logger.warning(f"Failed to capture screenshot for {url}: {e}")

        page.close()
        context.close()
        browser.close()

    return screenshot_urls

@app.route('/task_status')
def task_status():
    task_id = session.get('task_id')
    if not task_id or task_id not in tasks:
        return jsonify({'status': 'no_task'})

    task_info = tasks[task_id]
    return jsonify(task_info)

@app.route('/sitemap_content')
def sitemap_content():
    task_id = session.get('task_id')
    if not task_id or task_id not in tasks:
        return '<pre>No sitemap available.</pre>'

    sitemap_urls = tasks[task_id].get('sitemap_urls', [])
    sitemap_tree = build_sitemap_tree(sitemap_urls)
    return render_template('partials/sitemap_content.html', sitemap_tree=sitemap_tree)

@app.route('/screenshots_content')
def screenshots_content():
    task_id = session.get('task_id')
    if not task_id or task_id not in tasks:
        return '<div>No screenshots available.</div>'

    screenshot_urls = tasks[task_id].get('screenshot_urls', [])
    return render_template('partials/screenshots_content.html', screenshot_urls=screenshot_urls)

@app.route('/api_calls_content')
def api_calls_content():
    task_id = session.get('task_id')
    if not task_id or task_id not in tasks:
        return '<div>No API calls available.</div>'

    api_calls = tasks[task_id].get('api_calls', [])
    return render_template('partials/api_calls_content.html', api_calls=api_calls)

# Error handlers
@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', error_message='Page not found.'), 404

@app.errorhandler(500)
def internal_server_error(e):
    logger.error(f"Server Error: {e}, Path: {request.path}")
    return render_template('error.html', error_message='An internal server error occurred.'), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)

