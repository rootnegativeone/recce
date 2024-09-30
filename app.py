from flask import Flask, render_template, request, redirect, url_for
import os
import uuid
import json
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form['url']
        task_id = str(uuid.uuid4())
        sitemap_file = generate_sitemap(url, task_id)
        capture_screenshots(sitemap_file, task_id)
        return redirect(url_for('results', task_id=task_id))
    return render_template('index.html')

@app.route('/results/<task_id>')
def results(task_id):
    sitemap_link = f'static/results/{task_id}/sitemap.json'
    screenshots_dir = f'static/results/{task_id}/screenshots'
    screenshots = [
        f'results/{task_id}/screenshots/{f}'
        for f in os.listdir(screenshots_dir)
        if f.endswith('.png')
    ]
    return render_template('results.html', sitemap_link=sitemap_link, screenshots=screenshots)

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
        browser = p.chromium.launch()
        context = browser.new_context()
        for url in urls:
            page = context.new_page()
            try:
                page.goto(url, timeout=10000)
                filename = (
                    url.replace('://', '_')
                    .replace('/', '_')
                    .replace('?', '_')
                    .replace('&', '_')
                    + '.png'
                )
                filepath = os.path.join(screenshots_dir, filename)
                page.screenshot(path=filepath, full_page=True)
            except Exception as e:
                print(f"Failed to capture screenshot for {url}: {e}")
                continue
        browser.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

