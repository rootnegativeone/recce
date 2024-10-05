from flask import Flask, render_template, request, redirect, url_for
import os
import uuid
import json
import requests
import boto3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import logging

app = Flask(__name__)
s3 = boto3.client('s3', region_name='ca-central-1')

# Set up logging (optional but recommended)
logging.basicConfig(level=logging.ERROR)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            # Your existing code to process the form
            url = request.form['url']
            task_id = generate_task_id()
            sitemap_file = f'sitemaps/{task_id}.json'
            # Call your functions
            generate_sitemap(url, sitemap_file)
            capture_screenshots(sitemap_file, task_id)
            # Redirect or render success template
            return redirect(url_for('results', task_id=task_id))
        except Exception as e:
            app.logger.error(f"Error processing request: {e}")
            return render_template('error.html', error_message=str(e)), 500
    return render_template('index.html')

@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', error_message='Page not found.'), 404

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"Server Error: {e}, Path: {request.path}")
    return render_template('error.html', error_message='An internal server error occurred.'), 500

@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors
    if isinstance(e, HTTPException):
        return e

    # Log the error
    app.logger.error(f"Unhandled Exception: {e}")

    # Show detailed error only in development
    if app.config.get("ENV") == "development":
        return render_template('error.html', error_message=str(e)), 500
    else:
        return render_template('error.html', error_message='An internal error occurred.'), 500

@app.route('/trigger-error')
def trigger_error():
    raise Exception('This is a test error!')

@app.route('/results/<task_id>')
def results(task_id):
    sitemap_file = f'static/results/{task_id}/sitemap.json'
    screenshots_dir = f'static/results/{task_id}/screenshots'

    # Upload sitemap to S3
    sitemap_object_name = f'{task_id}/sitemap.json'
    upload_file_to_s3(sitemap_file, 'recce-results', sitemap_object_name)
    sitemap_url = generate_presigned_url('recce-results', sitemap_object_name, expiration=86400)  # 24 hours

    # Upload screenshots to S3 and generate URLs
    screenshot_urls = []
    for filename in os.listdir(screenshots_dir):
        if filename.endswith('.png'):
            file_path = os.path.join(screenshots_dir, filename)
            object_name = f'{task_id}/screenshots/{filename}'
            upload_file_to_s3(file_path, 'recce-results', object_name)
            url = generate_presigned_url('recce-results', object_name, expiration=86400)
            screenshot_urls.append(url)

    return render_template('results.html', sitemap_url=sitemap_url, screenshot_urls=screenshot_urls)

def generate_sitemap(start_url, task_id):
    visited = set()
    to_visit = [start_url]
    sitemap = []
    base_url = '{uri.scheme}://{uri.netloc}'.format(uri=requests.utils.urlparse(start_url))

    while to_visit:
        url = to_visit.pop()
        if url in visited or len(visited) >= 50:  # Limit to 50 pages
            continue
        visited.add(url)
        sitemap.append(url)
        try:
            response = requests.get(url)
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
            print(f"Error crawling {url}: {e}")
            continue

    # Create directory for the task
    result_dir = f'static/results/{task_id}'
    os.makedirs(result_dir, exist_ok=True)
    sitemap_file = f'{result_dir}/sitemap.json'

    # Save sitemap to file
    with open(sitemap_file, 'w') as f:
        json.dump(sitemap, f)

    return sitemap_file

def capture_screenshots(sitemap_file, task_id):
    with open(sitemap_file, 'r') as f:
        urls = json.load(f)

    result_dir = f'static/results/{task_id}'
    screenshots_dir = f'{result_dir}/screenshots'
    os.makedirs(screenshots_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        for idx, url in enumerate(urls):
            page = context.new_page()
            try:
                page.goto(url, timeout=10000)
                # Capture the screenshot
                screenshot_path = os.path.join(screenshots_dir, f'screenshot_{idx + 1}.png')
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception as e:
                print(f"Failed to capture screenshot for {url}: {e}")
            finally:
                page.close()
        context.close()
        browser.close()

def upload_file_to_s3(file_path, bucket_name, object_name):
    s3.upload_file(file_path, bucket_name, object_name)

def generate_presigned_url(bucket_name, object_name, expiration=3600):
    response = s3.generate_presigned_url('get_object',
                                         Params={'Bucket': bucket_name, 'Key': object_name},
                                         ExpiresIn=expiration)
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))

