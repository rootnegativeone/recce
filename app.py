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
import openai
try:
    import graphviz
except ImportError:
    print("Warning: graphviz package not available")
    graphviz = None
import stripe

app = Flask(__name__)
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
app.secret_key = 'development-secret-key'  # Fixed secret key for development
app.config['WTF_CSRF_ENABLED'] = False  # Disable CSRF for testing
app.config['SESSION_COOKIE_SECURE'] = False  # Allow session cookie over HTTP
app.config['SESSION_COOKIE_HTTPONLY'] = False  # Allow JS access to session cookie
app.config['SESSION_COOKIE_SAMESITE'] = None  # Allow cross-site requests
s3 = boto3.client('s3', region_name='ca-central-1')  

# Initialize OpenAI client
openai.api_key = os.getenv('OPENAI_API_KEY')

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
            logger.info(f"Received POST request with form data: {request.form}")
            url = request.form.get('url')
            concept1 = request.form.get('concept1')
            concept2 = request.form.get('concept2')
            logger.info(f"Parsed values - url: {url}, concept1: {concept1}, concept2: {concept2}")
            logger.info(f"Session before update: {dict(session)}")
            
            # Store concepts in session
            session['concept1'] = concept1
            session['concept2'] = concept2
            task_id = generate_task_id()
            session['task_id'] = task_id
            session.modified = True
            logger.info(f"Stored in session - concept1: {session.get('concept1')}, concept2: {session.get('concept2')}")
            logger.info(f"Session contents: {dict(session)}")
            
            # Require at least URL or both concepts
            if not url and not (concept1 and concept2):
                return render_template('error.html', error_message="Please provide either a URL or both system concepts"), 400
                
            # Validate URL if provided
            if url:
                try:
                    parsed = urlparse(url)
                    if not parsed.netloc:
                        if not url.startswith(('http://', 'https://')):
                            url = 'https://' + url
                        parsed = urlparse(url)
                        if not parsed.netloc:
                            raise ValueError("Invalid URL format")
                    
                    # Test if URL is reachable
                    if url:  # Only test if URL is provided
                        response = requests.head(url, timeout=5)
                        response.raise_for_status()
                except (ValueError, requests.RequestException) as e:
                    logger.error(f"URL validation error: {e}")
                    return render_template('error.html', error_message=f"Unable to access URL: {str(e)}"), 400
            
            # Store concepts in session
            session['concept1'] = concept1
            session['concept2'] = concept2
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
        capture_screenshots(task_id, urls)
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

def capture_screenshots_task(task_id, urls):
    processed_urls = set()  # Keep track of processed URLs to prevent duplicates
    screenshot_urls = []
    tasks[task_id]['api_calls'] = []
    logger.info(f"Starting screenshot capture for task {task_id}")
    logger.info(f"Initial API calls count: {len(tasks[task_id].get('api_calls', []))}")
    logger.info(f"Starting screenshot capture for task {task_id}")
    logger.info(f"Starting screenshot capture for task {task_id}")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 720})
            page = context.new_page()
        except Exception as e:
            logger.error(f"Failed to launch browser: {e}")
            return []
        context = browser.new_context(viewport={'width': 1280, 'height': 720})

        def log_api_request(request):
            if request.resource_type in ["xhr", "fetch"]:
                api_call = {
                    'url': request.url,
                    'method': request.method,
                    'headers': dict(request.headers)
                }
                tasks[task_id]['api_calls'].append(api_call)
                logger.info(f"Captured API call: {request.method} {request.url}")
                logger.info(f"Current API calls count: {len(tasks[task_id].get('api_calls', []))}")
                logger.info(f"API call details: {api_call}")
                logger.info(f"Current API calls count: {len(tasks[task_id].get('api_calls', []))}")
                logger.info(f"API call details: {api_call}")

        context.on("request", log_api_request)
        page = context.new_page()

        for idx, url in enumerate(urls):
            if url in processed_urls:
                continue
            processed_urls.add(url)

            try:
                if url:  # Only try to capture screenshot if URL is provided
                    page.goto(url, timeout=30000)
                page.wait_for_load_state('networkidle')
                page.wait_for_selector('img', state='visible', timeout=10000)
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(2000)
                page.evaluate('window.scrollTo(0, 0)')
                page.wait_for_timeout(1000)

                # Get page title for filename
                title = page.title()
                safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_'))[:50]
                screenshot_filename = f'{safe_title}_{idx + 1}.png'
                object_name = f'{task_id}/screenshots/{screenshot_filename}'

                # Take screenshot and upload to S3
                screenshot = page.screenshot(full_page=True)
                s3.put_object(Bucket='recce-results', Key=object_name, Body=screenshot, ContentType='image/png')
                screenshot_url = generate_presigned_url('recce-results', object_name, expiration=86400)

                screenshot_urls.append({'url': screenshot_url, 'filename': screenshot_filename})
                tasks[task_id]['screenshot_urls'] = screenshot_urls
                logger.info(f"Captured and uploaded screenshot for {url}")
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

import openai
import graphviz

# Function to generate a system diagram
def generate_system_diagram(concept1, concept2):
    if not graphviz:
        logger.error("Graphviz not available")
        return None
        
    try:
        logger.info(f"Generating system diagram with concepts: {concept1}, {concept2}")
        if not os.getenv('OPENAI_API_KEY'):
            logger.error("OpenAI API key not set")
            return None
        if concept1 and concept2:
            logger.info("Using concept analysis")
        else:
            logger.info("Using default diagram")
        logger.info("Starting diagram generation...")
        logger.info("Using default diagram" if not concept1 and not concept2 else "Using concept analysis")
        logger.info("Starting diagram generation...")
        logger.info("Using default diagram" if not concept1 and not concept2 else "Using concept analysis")
        logger.info("Starting diagram generation...")
        
        # Create diagram
        dot = graphviz.Digraph(format='svg')
        dot.attr(rankdir='LR')
        
        if concept1 or concept2:
            # Get system analysis from OpenAI
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[{
                    "role": "system",
                    "content": "You are a systems analyst. Given two concepts, describe their relationship in a system."
                }, {
                    "role": "user", 
                    "content": f"Analyze the relationship between {concept1} and {concept2} in a system. "
                              f"Respond with only 2-3 key connections between them."
                }]
            )
            
            # Add nodes and relationships from LLM
            dot.node(concept1, concept1)
            dot.node(concept2, concept2)
            relationships = response.choices[0].message.content.split('\n')
            for i, rel in enumerate(relationships[:3]):
                dot.edge(concept1, concept2, rel)
        else:
            # Default diagram showing the site analysis system
            dot.attr(rankdir='LR')
            dot.attr(size='8,8!')  # Force diagram size
            dot.attr(ratio='fill')  # Fill available space
            dot.node('Browser', 'Browser\n(Screenshots + API Calls)')
            dot.node('Flask', 'Flask Server\n(URL Processing)')
            dot.node('S3', 'AWS S3\n(Image Storage)')
            dot.node('OpenAI', 'OpenAI API\n(Systems Analysis)')
            
            # Show data flow with compact spacing
            dot.edge('Browser', 'Flask', 'URL Analysis Request')
            dot.edge('Flask', 'Browser', 'Sitemap Data')
            dot.edge('Flask', 'S3', 'Store Screenshots')
            dot.edge('S3', 'Browser', 'Load Images')
            dot.edge('Flask', 'OpenAI', 'System Analysis')
            dot.edge('OpenAI', 'Browser', 'System Insights')
            
            svg_output = dot.pipe().decode('utf-8')
            logger.info("Successfully generated system diagram")
            logger.info(f"SVG output length: {len(svg_output) if svg_output else 0} characters")
            return svg_output
    except Exception as e:
        logger.error(f"Error generating system diagram: {e}")
        return None

@app.route('/systems_diagram')
def systems_diagram():
    try:
        task_id = session.get('task_id')
        logger.info("Systems diagram endpoint called")
        concept1 = session.get('concept1')
        concept2 = session.get('concept2')
        logger.info(f"Retrieved from session - concept1: {concept1}, concept2: {concept2}")
        logger.info(f"Session contents: {dict(session)}")
        logger.info(f"Session ID: {request.cookies.get('session')}")
        if not task_id or task_id not in tasks:
            logger.info("Generating default system diagram")
            logger.info("Starting diagram generation...")
            diagram_svg = generate_system_diagram(None, None)  # Generate default diagram
            logger.info("Default diagram generated successfully" if diagram_svg else "Failed to generate default diagram")
            return render_template('partials/systems_diagram.html', diagram_svg=diagram_svg)
            
        concept1 = session.get('concept1')
        concept2 = session.get('concept2')
            
        diagram_svg = generate_system_diagram(concept1, concept2)
        if not task_id or task_id not in tasks:
            logger.info("Generating default system diagram")
            logger.info("Starting diagram generation...")
            diagram_svg = generate_system_diagram(None, None)  # Generate default diagram
            logger.info("Default diagram generated successfully" if diagram_svg else "Failed to generate default diagram")
            return render_template('partials/systems_diagram.html', diagram_svg=diagram_svg)
            
        concept1 = session.get('concept1')
        concept2 = session.get('concept2')
            
        diagram_svg = generate_system_diagram(concept1, concept2)
        return render_template('partials/systems_diagram.html', diagram_svg=diagram_svg)
    except Exception as e:
        logger.error(f"Error generating system diagram: {e}")
        return '<pre>Error generating system diagram</pre>'

@app.route('/api_calls_content')
def api_calls_content():
    task_id = session.get('task_id')
    if not task_id or task_id not in tasks:
        logger.info("No task_id found for API calls")
        return '<div>No API calls available.</div>'

    api_calls = tasks[task_id].get('api_calls', [])
    logger.info(f"Found {len(api_calls)} API calls for task {task_id}")
    logger.info(f"API calls: {api_calls}")
    # Deduplicate API calls
    seen = set()
    unique_calls = []
    for call in api_calls:
        key = call['method'] + ' ' + call['url'].split('?')[0]
        if key not in seen:
            seen.add(key)
            unique_calls.append(call)
    return render_template('partials/api_calls_content.html', api_calls=unique_calls)
    # Deduplicate API calls
    seen = set()
    unique_calls = []
    for call in api_calls:
        key = call['method'] + ' ' + call['url'].split('?')[0]
        if key not in seen:
            seen.add(key)
            unique_calls.append(call)
    return render_template('partials/api_calls_content.html', api_calls=unique_calls)

# Error handlers
@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', error_message='Page not found.'), 404

@app.errorhandler(500)
def internal_server_error(e):
    logger.error(f"Server Error: {e}, Path: {request.path}")
    return render_template('error.html', error_message='An internal server error occurred.'), 500

@app.route('/account')
def account():
    # Basic account info endpoint
    if 'user_id' not in session:
        return jsonify({'status': 'not_logged_in'})
    return jsonify({'status': 'ok'})

@app.route('/login', methods=['POST'])
def login():
    try:
        email = request.form.get('email')
        password = request.form.get('password')
        # TODO: Add actual auth logic
        session['user_id'] = email  # Temporary for testing
        return jsonify({'status': 'success'})
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'error': str(e)}), 401

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': os.getenv('STRIPE_PRICE_ID'),
                'quantity': 1,
            }],
            mode='subscription',
            success_url=request.host_url + 'success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'cancelled',
        )
        return jsonify({'sessionId': checkout_session.id})
    except Exception as e:
        logger.error(f"Error creating checkout session: {e}")
        return jsonify({'error': str(e)}), 403

@app.route('/success')
def success():
    session_id = request.args.get('session_id')
    if session_id:
        try:
            # Verify the session with Stripe
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if checkout_session.payment_status == "paid":
                # Store subscription info in session
                session['subscribed'] = True
                session['subscription_id'] = checkout_session.subscription
                return render_template('index.html', message="Subscription successful!")
        except Exception as e:
            logger.error(f"Error verifying subscription: {e}")
    return redirect(url_for('index'))

@app.route('/cancelled')
def cancelled():
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
