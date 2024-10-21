from flask import Flask, render_template, request
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

app = Flask(__name__)
s3 = boto3.client('s3', region_name='ca-central-1')  

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def generate_task_id():
    return str(uuid.uuid4())

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            url = request.form['url']
            task_id = generate_task_id()
            logger.info(f"Processing URL: {url} with Task ID: {task_id}")

            # Generate sitemap
            sitemap_urls = generate_sitemap(url, task_id)
            logger.info(f"Sitemap generated with {len(sitemap_urls)} URLs.")

            # Capture screenshots
            screenshot_urls = capture_screenshots(task_id, sitemap_urls)
            logger.info(f"Captured {len(screenshot_urls)} screenshots.")

            # Build sitemap tree
            sitemap_tree = build_sitemap_tree(sitemap_urls)

            # Current year for footer
            current_year = datetime.now().year

            # Render template with results
            return render_template(
                'index.html',
                analysis_complete=True,
                sitemap_tree=sitemap_tree,
                screenshot_urls=screenshot_urls,
                current_year=current_year
            )
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            return render_template('error.html', error_message=str(e)), 500
    else:
        current_year = datetime.now().year
        return render_template('index.html', analysis_complete=False, current_year=current_year)

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

    # Create directory for the task
    result_dir = f'static/results/{task_id}'
    os.makedirs(result_dir, exist_ok=True)
    sitemap_file = f'{result_dir}/sitemap.json'

    # Save sitemap to file (optional)
    with open(sitemap_file, 'w') as f:
        json.dump(sitemap, f)

    return sitemap

def build_sitemap_tree(urls):
    nodes = {}
    root = Node('root')
    nodes['/'] = root

    for url in urls:
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.strip('/').split('/') if part]
        current_parent = root

        for part in path_parts:
            if part not in nodes:
                nodes[part] = Node(part, parent=current_parent)
            current_parent = nodes[part]

    # Render the tree into a list of strings
    sitemap_tree = []
    for pre, fill, node in RenderTree(root):
        sitemap_tree.append(f"{pre}{node.name}")

    return sitemap_tree

def capture_screenshots(task_id, urls):
    result_dir = f'static/results/{task_id}'
    screenshots_dir = f'{result_dir}/screenshots'
    os.makedirs(screenshots_dir, exist_ok=True)

    screenshot_urls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        for idx, url in enumerate(urls):
            try:
                page.goto(url, timeout=30000)
                screenshot_filename = f'screenshot_{idx + 1}.png'
                screenshot_path = os.path.join(screenshots_dir, screenshot_filename)
                page.screenshot(path=screenshot_path, full_page=True)

                # Upload screenshot to S3
                object_name = f'{task_id}/screenshots/{screenshot_filename}'
                upload_file_to_s3(screenshot_path, 'recce-results', object_name)  # Replace 
                screenshot_url = generate_presigned_url('recce-results', object_name, expiration=86400)

                screenshot_urls.append({'url': screenshot_url, 'filename': screenshot_filename})
                logger.info(f"Captured screenshot for {url}")
            except Exception as e:
                logger.warning(f"Failed to capture screenshot for {url}: {e}")
        page.close()
        context.close()
        browser.close()

    return screenshot_urls

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

