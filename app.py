from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.utils import secure_filename
from sqlalchemy import text, func
import json
import os
import csv
import re
import socket
import ipaddress
from urllib.parse import urlparse
import time
import ssl
import urllib.request
import urllib.error
import subprocess
import shutil
import uuid
import threading

app = Flask(__name__)
# Определяем путь к базе данных в зависимости от окружения
if os.path.exists('/app'):
    # Docker окружение
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////app/instance/website_scanner.db'
else:
    # Локальное окружение (Linux/Mac/Windows) — используем каталог instance рядом с app.py
    base_dir = os.path.dirname(os.path.abspath(__file__))
    instance_dir = os.path.join(base_dir, 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    db_path = os.path.join(instance_dir, 'website_scanner.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'your-secret-key-here'

# Конфигурация для загрузки файлов
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads', 'screenshots')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Создаем папку для загрузок если её нет
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _get_project_screenshots_dir(project_id):
    if project_id is None:
        return app.config['UPLOAD_FOLDER'], 'uploads/screenshots'
    try:
        pid = int(project_id)
    except Exception:
        return app.config['UPLOAD_FOLDER'], 'uploads/screenshots'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], f'project_{pid}')
    return folder, f'uploads/screenshots/project_{pid}'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def delete_screenshot_file(rel_path):
    try:
        if not rel_path:
            return
        static_root = os.path.abspath(os.path.join(app.root_path, 'static'))
        uploads_root = os.path.abspath(app.config['UPLOAD_FOLDER'])
        raw = str(rel_path).strip().lstrip('/').replace('\\', '/')

        def safe_abs_join(base, subpath):
            try:
                ap = os.path.abspath(os.path.normpath(os.path.join(base, subpath)))
                if os.path.commonpath([base, ap]) != base:
                    return None
                return ap
            except Exception:
                return None

        candidates = []
        if raw.startswith('uploads/screenshots/'):
            sub = raw[len('uploads/screenshots/'):]
            p1 = safe_abs_join(uploads_root, sub)
            if p1:
                candidates.append(p1)
            p2 = safe_abs_join(static_root, raw)
            if p2:
                candidates.append(p2)
        elif raw.startswith('static/uploads/screenshots/'):
            sub = raw[len('static/uploads/screenshots/'):]
            p1 = safe_abs_join(uploads_root, sub)
            if p1:
                candidates.append(p1)
            p2 = safe_abs_join(app.root_path, raw)
            if p2:
                candidates.append(p2)
        else:
            fname = os.path.basename(raw)
            p1 = safe_abs_join(uploads_root, fname)
            if p1:
                candidates.append(p1)
            p2 = safe_abs_join(static_root, os.path.join('uploads', 'screenshots', fname))
            if p2:
                candidates.append(p2)
        seen = set()
        for p in candidates:
            try:
                ap = os.path.abspath(p)
                if ap in seen:
                    continue
                seen.add(ap)
                if os.path.exists(ap) and os.path.isfile(ap):
                    try:
                        os.remove(ap)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

def _which_browser():
    for bin_name in ['google-chrome', 'chromium', 'chromium-browser']:
        p = shutil.which(bin_name)
        if p:
            return p
    return None

def _which_wkhtmltoimage():
    return shutil.which('wkhtmltoimage')

def capture_screenshot_headless(url, headers=None, width=1366, height=768, delay_ms=2000, project_id=None):
    try:
        SCREENSHOT_SEM.acquire()
    except Exception:
        pass
    headers = headers or {}
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    rnd = uuid.uuid4().hex[:8]
    filename = f'{ts}_{rnd}.png'
    out_dir, rel_dir = _get_project_screenshots_dir(project_id)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass
    out_path = os.path.join(out_dir, filename)
    rel_path = f'{rel_dir}/{filename}'
    chrome_bin = _which_browser()
    wk = _which_wkhtmltoimage()
    has_extra_headers = False
    try:
        if isinstance(headers, dict):
            for k in headers.keys():
                if str(k).lower() != 'user-agent':
                    has_extra_headers = True
                    break
    except Exception:
        has_extra_headers = False

    def try_chrome():
        if not chrome_bin:
            return None
        user_dir = os.path.join('/tmp', 'chrome-data-' + uuid.uuid4().hex)
        try:
            os.makedirs(user_dir, exist_ok=True)
        except Exception:
            pass
        cmd = [chrome_bin, '--headless=new', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage', '--disable-crashpad', f'--user-data-dir={user_dir}', '--no-first-run', '--no-default-browser-check', '--ignore-certificate-errors', '--allow-insecure-localhost', '--allow-running-insecure-content', '--disable-web-security', '--virtual-time-budget=8000', '--hide-scrollbars', f'--screenshot={out_path}', f'--window-size={int(width)},{int(height)}']
        ua = None
        if isinstance(headers, dict):
            for k, v in headers.items():
                if str(k).lower() == 'user-agent':
                    ua = v
                    break
        if not ua:
            ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        if ua:
            cmd.append(f'--user-agent={ua}')
        cmd.append(url)
        ret_path = None
        try:
            env = os.environ.copy()
            env['XDG_RUNTIME_DIR'] = '/tmp'
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, env=env)
            if res.returncode == 0 and os.path.exists(out_path):
                ret_path = rel_path
            try:
                if not ret_path:
                    print('CHROME_FAIL', res.returncode, res.stderr.decode(errors='ignore')[:500])
            except Exception:
                pass
        except Exception:
            pass
        try:
            shutil.rmtree(user_dir, ignore_errors=True)
        except Exception:
            pass
        return ret_path

    def try_wk():
        if not wk:
            return None
        cmd = [wk, '--load-error-handling', 'ignore', '--ssl-protocol', 'any']
        cookies = []
        if isinstance(headers, dict):
            for k, v in headers.items():
                kl = str(k).strip()
                vl = str(v).strip()
                if kl.lower() == 'cookie':
                    try:
                        parts = [p.strip() for p in vl.split(';') if p.strip()]
                        for p in parts:
                            if '=' in p:
                                name, val = p.split('=', 1)
                                name = name.strip()
                                val = val.strip()
                                if name:
                                    cookies.append((name, val))
                    except Exception:
                        pass
                else:
                    cmd.extend(['--custom-header', kl, vl])
        for (name, val) in cookies:
            cmd.extend(['--cookie', name, val])
        if delay_ms and int(delay_ms) > 0:
            cmd.extend(['--javascript-delay', str(int(delay_ms))])
        cmd.extend(['--width', str(width), '--quality', '85', url, out_path])
        try:
            env = os.environ.copy()
            env['QT_QPA_PLATFORM'] = 'offscreen'
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, env=env)
            if res.returncode == 0 and os.path.exists(out_path):
                return rel_path
            try:
                print('WK_FAIL', res.returncode, res.stderr.decode(errors='ignore')[:500])
            except Exception:
                pass
        except Exception:
            return None
        return None

    def _save_meta(out_path, meta):
        try:
            mp = out_path.rsplit('.', 1)[0] + '.json'
            with open(mp, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception:
            pass

    def try_uc():
        try:
            import undetected_chromedriver as uc
            from selenium.webdriver.chrome.options import Options
            opts = uc.ChromeOptions()
            opts.add_argument('--headless=new')
            opts.add_argument('--disable-gpu')
            opts.add_argument(f'--window-size={int(width)},{int(height)}')
            opts.add_argument('--disable-blink-features=AutomationControlled')
            opts.add_argument('--hide-scrollbars')
            opts.add_argument('--ignore-certificate-errors')
            opts.add_argument('--allow-insecure-localhost')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            try:
                opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
            except Exception:
                pass
            uc_user_dir = os.path.join('/tmp', 'uc-data-' + uuid.uuid4().hex)
            try:
                os.makedirs(uc_user_dir, exist_ok=True)
            except Exception:
                pass
            opts.add_argument(f'--user-data-dir={uc_user_dir}')
            try:
                print('UC_START')
            except Exception:
                pass
            driver = uc.Chrome(options=opts, headless=True)
            try:
                ua = None
                extra_headers = {}
                cookies = []
                if isinstance(headers, dict):
                    for k, v in headers.items():
                        kl = str(k).strip()
                        vl = str(v).strip()
                        if kl.lower() == 'user-agent':
                            ua = vl
                        elif kl.lower() == 'cookie':
                            try:
                                parts = [p.strip() for p in vl.split(';') if p.strip()]
                                for p in parts:
                                    if '=' in p:
                                        name, val = p.split('=', 1)
                                        name = name.strip()
                                        val = val.strip()
                                        if name:
                                            cookies.append((name, val))
                            except Exception:
                                pass
                        else:
                            extra_headers[kl] = vl
                try:
                    if 'Accept-Language' not in extra_headers and 'accept-language' not in [k.lower() for k in extra_headers.keys()]:
                        extra_headers['Accept-Language'] = 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
                except Exception:
                    pass
                try:
                    driver.execute_cdp_cmd('Network.enable', {})
                except Exception:
                    pass
                try:
                    driver.execute_cdp_cmd('Page.enable', {})
                except Exception:
                    pass
                try:
                    driver.execute_cdp_cmd('Security.enable', {})
                    driver.execute_cdp_cmd('Security.setIgnoreCertificateErrors', {"ignore": True})
                except Exception:
                    pass
                if not ua:
                    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                if ua:
                    try:
                        driver.execute_cdp_cmd('Network.setUserAgentOverride', {"userAgent": ua})
                    except Exception:
                        pass
                if extra_headers:
                    try:
                        driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {"headers": extra_headers})
                    except Exception:
                        pass
                try:
                    print('UC_GET', url)
                except Exception:
                    pass
                driver.get(url)
                try:
                    time.sleep(0.3)
                    try:
                        from selenium.webdriver.common.by import By
                        btn = driver.find_element(By.ID, 'details-button')
                        try:
                            btn.click()
                        except Exception:
                            pass
                        try:
                            link = driver.find_element(By.ID, 'proceed-link')
                            link.click()
                        except Exception:
                            pass
                    except Exception:
                        pass
                    try:
                        from selenium.webdriver.common.keys import Keys
                        body = driver.find_element(By.TAG_NAME, 'body')
                        body.send_keys('thisisunsafe')
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    host = urlparse(url).hostname or ''
                    for name, val in cookies:
                        try:
                            driver.add_cookie({"name": name, "value": val, "domain": host})
                        except Exception:
                            pass
                    if cookies:
                        driver.get(url)
                except Exception:
                    pass
                if delay_ms and int(delay_ms) > 0:
                    try:
                        time.sleep(int(delay_ms) / 1000.0)
                    except Exception:
                        pass
                try:
                    t0 = time.time()
                    while time.time() - t0 < 10:
                        try:
                            st = driver.execute_script('return document.readyState')
                            if st == 'complete':
                                break
                        except Exception:
                            pass
                        time.sleep(0.3)
                except Exception:
                    pass
                driver.set_window_size(int(width), int(height))
                png = None
                try:
                    res = driver.execute_cdp_cmd('Page.captureScreenshot', {"format":"png","fromSurface": True})
                    if isinstance(res, dict) and res.get('data'):
                        import base64
                        png = base64.b64decode(res['data'])
                except Exception:
                    pass
                if not png:
                    try:
                        png = driver.get_screenshot_as_png()
                    except Exception:
                        driver.execute_script('try{document.body.style.background="white";}catch(e){}')
                        png = driver.get_screenshot_as_png()
                with open(out_path, 'wb') as f:
                    f.write(png)
                ret_path = None
                meta = {}
                if os.path.exists(out_path):
                    try:
                        print('UC_DONE', out_path)
                    except Exception:
                        pass
                    ret_path = rel_path
                    try:
                        logs = driver.get_log('performance')
                        status = None
                        final_url = None
                        for entry in logs:
                            try:
                                msg = json.loads(entry.get('message') or '{}').get('message') or {}
                                if msg.get('method') == 'Network.responseReceived':
                                    p = msg.get('params') or {}
                                    if str(p.get('type') or '').lower() == 'document':
                                        resp = p.get('response') or {}
                                        status = resp.get('status', status)
                                        final_url = resp.get('url', final_url)
                            except Exception:
                                pass
                        if status is not None or final_url:
                            meta = {'status_code': status, 'final_url': final_url}
                            _save_meta(out_path, meta)
                    except Exception:
                        pass
                
            except Exception as e:
                try:
                    print('UC_FAIL', str(e)[:400])
                except Exception:
                    pass
            try:
                driver.quit()
                try:
                    svc = getattr(driver, 'service', None)
                    proc = getattr(svc, 'process', None)
                    if proc:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        try:
                            proc.kill()
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass
            try:
                shutil.rmtree(uc_user_dir, ignore_errors=True)
            except Exception:
                pass
            return ret_path
        except Exception:
            pass
        return None

    # Предпочитаем wkhtmltoimage для дополнительных заголовков, иначе Chrome
    shot_path = None
    variants = [url]
    try:
        parsed = urlparse(url)
        if parsed.scheme == 'https':
            http_variant = parsed._replace(scheme='http').geturl()
            variants.append(http_variant)
    except Exception:
        pass
    prefer_uc = os.path.exists('/app')
    for v in variants:
        url = v
        if prefer_uc:
            shot_path = try_uc() or try_chrome() or (wk and try_wk())
        else:
            if has_extra_headers:
                shot_path = try_uc() or (wk and try_wk()) or try_chrome()
            else:
                shot_path = try_chrome() or (wk and try_wk()) or try_uc()
        if shot_path:
            break
    try:
        SCREENSHOT_SEM.release()
    except Exception:
        pass
    return shot_path

def parse_raw_request(raw_request):
    """Парсит raw HTTP запрос и извлекает основную информацию"""
    lines = raw_request.strip().split('\n')
    
    if not lines:
        raise ValueError("Пустой запрос")
    
    # Парсим первую строку (метод, URL, версия HTTP)
    request_line = lines[0].strip()
    parts = request_line.split(' ')
    
    if len(parts) < 2:
        raise ValueError("Неверный формат первой строки запроса")
    
    method = parts[0]
    url = parts[1]
    
    # Парсим заголовки
    headers = {}
    body_start = len(lines)
    
    for i, line in enumerate(lines[1:], 1):
        line = line.strip()
        if not line:  # Пустая строка означает конец заголовков
            body_start = i + 1
            break
        
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()
    
    # Извлекаем тело запроса (если есть)
    body = ''
    if body_start < len(lines):
        body = '\n'.join(lines[body_start:])
    
    # Парсим параметры из URL и тела
    parameters = ''
    
    # Параметры из URL (GET параметры)
    if '?' in url:
        url_parts = url.split('?', 1)
        url = url_parts[0]
        parameters = url_parts[1]
    
    # Параметры из тела (POST данные)
    if body and method.upper() in ['POST', 'PUT', 'PATCH']:
        if parameters:
            parameters += '\n\nBody:\n' + body
        else:
            parameters = 'Body:\n' + body
    
    # Форматируем заголовки для сохранения
    headers_str = '\n'.join([f"{k}: {v}" for k, v in headers.items()])
    
    return {
        'method': method.upper(),
        'url': url,
        'headers': headers_str,
        'parameters': parameters
    }

def parse_multiple_technologies(tech_string):
    """Парсит строку технологий и возвращает список кортежей (название, версия)"""
    technologies = []
    
    # Известные технологии для распознавания
    known_techs = ['PHP', 'Laravel', 'Apache', 'Nginx', 'MySQL', 'PostgreSQL', 'Redis', 'Node.js', 'React', 'Vue', 'Angular', 'Django', 'Flask', 'Express', 'MongoDB', 'Docker', 'Kubernetes', 'Jenkins', 'Git', 'Python', 'JavaScript', 'TypeScript', 'Java', 'C#', 'Ruby', 'Go', 'Rust', 'Swift', 'Kotlin']
    
    # Сначала пробуем разделить по пробелам
    words = tech_string.split()
    
    i = 0
    while i < len(words):
        word = words[i]
        tech_name = word
        tech_version = ''
        
        # Проверяем, является ли слово известной технологией
        if word in known_techs:
            # Проверяем следующее слово на версию
            if i + 1 < len(words):
                next_word = words[i + 1]
                # Убираем скобки и префиксы версий
                clean_next = next_word.strip('()').replace('v', '').replace('V', '')
                if clean_next and (clean_next[0].isdigit() or clean_next.startswith('.')):
                    tech_version = clean_next
                    i += 1  # Пропускаем следующее слово, так как это версия
            
            technologies.append((tech_name, tech_version))
        else:
            # Пробуем парсить как "название-версия" или "название версия"
            if '-' in word and not word.startswith('-'):
                parts = word.rsplit('-', 1)
                if len(parts) == 2 and parts[1].replace('.', '').isdigit():
                    tech_name = parts[0]
                    tech_version = parts[1]
                    technologies.append((tech_name, tech_version))
                else:
                    # Если не удалось распарсить версию, добавляем как есть
                    technologies.append((word, ''))
            elif any(char.isdigit() for char in word):
                # Если в слове есть цифры, пробуем выделить версию
                import re
                match = re.match(r'^([a-zA-Z]+)([0-9.]+)$', word)
                if match:
                    tech_name = match.group(1)
                    tech_version = match.group(2)
                    technologies.append((tech_name, tech_version))
                else:
                    technologies.append((word, ''))
            else:
                # Добавляем как технологию без версии
                technologies.append((word, ''))
        
        i += 1
    
    return technologies if technologies else [(tech_string, '')]

def extract_path_from_url(url):
    """Извлекает путь из URL для фаззинга"""
    try:
        parsed = urlparse(url)
        path = parsed.path
        # Сохраняем начальный слеш для корректного отображения полного пути
        return path if path else ""
    except Exception as e:
        print(f"Ошибка при извлечении пути из URL {url}: {e}")
        return ""

def parse_fuzz_csv(csv_file_path):
    """Парсит CSV файл с результатами фаззинга и классифицирует записи"""
    files = []
    directories = []
    routes = []
    
    try:
        with open(csv_file_path, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            total_rows = 0
            skipped_rows = 0
            
            for row in reader:
                total_rows += 1
                fuzz_value = row.get('FUZZ', '').strip()
                url = row.get('url', '').strip()
                status_code = int(row.get('status_code', 0))
                redirect_location = row.get('redirectlocation', '').strip()
                
                # Пропускаем только пустые записи
                if not fuzz_value:
                    skipped_rows += 1
                    continue
                
                # Извлекаем полный путь из URL
                full_path = extract_path_from_url(url)
                # Если не удалось извлечь путь из URL, используем FUZZ
                display_name = full_path if full_path else fuzz_value
                
                # Классификация по типу
                is_file = classify_as_file(fuzz_value)
                is_directory = classify_as_directory(fuzz_value, redirect_location)
                
                if is_file:
                    files.append({
                        'name': display_name,
                        'status_code': status_code,
                        'redirect': redirect_location
                    })
                elif is_directory:
                    directories.append({
                        'name': display_name,
                        'status_code': status_code,
                        'redirect': redirect_location
                    })
                else:
                    routes.append({
                        'name': display_name,
                        'status_code': status_code,
                        'redirect': redirect_location
                    })
            
            print(f"Обработано строк: {total_rows}, пропущено: {skipped_rows}")
            print(f"Классифицировано: файлы={len(files)}, каталоги={len(directories)}, маршруты={len(routes)}")
    
    except Exception as e:
        print(f"Ошибка при парсинге CSV: {e}")
        return [], [], []
    
    return files, directories, routes

def analyze_fuzz_csv_text(csv_text):
    """Анализирует CSV данные и возвращает статистику для фильтрации с группировкой уникальных типов ответов"""
    from io import StringIO
    from collections import defaultdict
    import hashlib
    
    stats = {
        'total_records': 0,
        'status_codes': defaultdict(int),
        'content_lengths': defaultdict(int),
        'content_lines': defaultdict(int),
        'content_words': defaultdict(int),
        'records_by_status': defaultdict(list),
        'response_groups': defaultdict(lambda: {'count': 0, 'examples': [], 'records': [], 'fingerprint': '', 'status_code': 0}),
        'unique_response_types': 0
    }
    
    try:
        print(f"Начинаем анализ CSV данных, длина: {len(csv_text)} символов")
        csv_file = StringIO(csv_text)
        reader = csv.DictReader(csv_file)
        
        for row in reader:
            stats['total_records'] += 1
            if stats['total_records'] <= 5:  # Логируем только первые 5 строк
                print(f"Обрабатываем строку {stats['total_records']}: {row}")
            
            fuzz_value = row.get('FUZZ', '').strip()
            url = row.get('url', '').strip()
            
            # Извлекаем полный путь из URL если он есть
            if url:
                full_path = extract_path_from_url(url)
                display_name = full_path if full_path else fuzz_value
            else:
                display_name = fuzz_value
            
            try:
                status_code = int(row.get('status_code', 0))
                content_length = int(row.get('content_length', 0))
                content_lines = int(row.get('content_lines', 0))
                content_words = int(row.get('content_words', 0))
            except ValueError as ve:
                print(f"Ошибка преобразования в int: {ve}, строка: {row}")
                continue
            redirect_location = row.get('redirectlocation', '').strip()
            
            # Создаем отпечаток ответа для группировки
            response_fingerprint = f"{status_code}:{content_length}:{content_lines}:{content_words}"
            if redirect_location:
                response_fingerprint += f":redirect"
            
            # Создаем хеш для уникальной идентификации группы
            fingerprint_hash = hashlib.md5(response_fingerprint.encode()).hexdigest()[:8]
            
            # Подсчет статистики
            stats['status_codes'][status_code] += 1
            stats['content_lengths'][content_length] += 1
            stats['content_lines'][content_lines] += 1
            stats['content_words'][content_words] += 1
            
            # Группировка по отпечаткам ответов
            group_key = fingerprint_hash
            stats['response_groups'][group_key]['count'] += 1
            stats['response_groups'][group_key]['fingerprint'] = response_fingerprint
            stats['response_groups'][group_key]['status_code'] = status_code
            
            # Создаем запись с правильными полями для импорта
            record = {
                'FUZZ': display_name,  # Используем полный путь вместо FUZZ значения
                'fuzz_value': display_name,  # Дублируем для совместимости
                'url': url,  # Добавляем URL для полноты
                'status_code': status_code,
                'content_length': content_length,
                'content_lines': content_lines,
                'content_words': content_words,
                'redirect': redirect_location,
                'redirectlocation': redirect_location  # Дублируем для совместимости
            }
            
            # Сохраняем ВСЕ записи для группы (не только примеры)
            stats['response_groups'][group_key]['records'].append(record)
            
            # Сохраняем примеры для группы (максимум 3 примера на группу для отображения)
            if len(stats['response_groups'][group_key]['examples']) < 3:
                stats['response_groups'][group_key]['examples'].append({
                    'fuzz': display_name,  # Используем полный путь
                    'status_code': status_code,
                    'content_length': content_length,
                    'content_lines': content_lines,
                    'content_words': content_words,
                    'redirect': redirect_location
                })
            
            # Сохраняем примеры записей для каждого статуса (для обратной совместимости)
            if len(stats['records_by_status'][status_code]) < 5:
                stats['records_by_status'][status_code].append({
                    'fuzz': display_name,  # Используем полный путь
                    'content_length': content_length,
                    'content_lines': content_lines,
                    'content_words': content_words,
                    'redirect': redirect_location
                })
        
        # Подсчитываем количество уникальных типов ответов
        stats['unique_response_types'] = len(stats['response_groups'])
        print(f"Анализ завершен: всего записей {stats['total_records']}, уникальных групп {stats['unique_response_types']}")
    
    except Exception as e:
        print(f"Ошибка при анализе CSV текста: {e}")
        return None
    
    return stats

def parse_fuzz_csv_text(csv_text, exclude_filters=None):
    """Парсит CSV данные из текста, классифицирует записи и применяет фильтры исключения"""
    from io import StringIO
    
    all_files = []
    all_directories = []
    all_routes = []
    
    try:
        # Сначала парсим ВСЕ записи из CSV
        csv_file = StringIO(csv_text)
        reader = csv.DictReader(csv_file)
        total_rows = 0
        
        for row in reader:
            total_rows += 1
            fuzz_value = row.get('FUZZ', '').strip()
            url = row.get('url', '').strip()
            status_code = int(row.get('status_code', 0))
            content_length = int(row.get('content_length', 0))
            content_lines = int(row.get('content_lines', 0))
            content_words = int(row.get('content_words', 0))
            redirect_location = row.get('redirectlocation', '').strip()
            
            # Пропускаем только пустые записи
            if not fuzz_value:
                continue
            
            # Извлекаем полный путь из URL
            full_path = extract_path_from_url(url)
            # Если не удалось извлечь путь из URL, используем FUZZ
            display_name = full_path if full_path else fuzz_value
            
            # Классификация по типу - используем display_name для правильной классификации
            is_file = classify_as_file(display_name)
            is_directory = classify_as_directory(display_name, redirect_location)
            
            record_data = {
                'name': display_name,
                'status_code': status_code,
                'content_length': content_length,
                'content_lines': content_lines,
                'content_words': content_words,
                'redirect': redirect_location
            }
            
            if is_file:
                all_files.append(record_data)
            elif is_directory:
                all_directories.append(record_data)
            else:
                all_routes.append(record_data)
        
        print(f"Всего обработано строк: {total_rows}")
        print(f"Всего записей: файлы={len(all_files)}, каталоги={len(all_directories)}, маршруты={len(all_routes)}")
        
        # Теперь применяем фильтры исключения, если они заданы
        if exclude_filters:
            filtered_files = []
            filtered_directories = []
            filtered_routes = []
            
            def should_exclude_record(record):
                """Проверяет, нужно ли исключить запись по фильтрам"""
                if record['status_code'] in exclude_filters.get('status_codes', []):
                    return True
                if record['content_length'] in exclude_filters.get('content_lengths', []):
                    return True
                if record['content_lines'] in exclude_filters.get('content_lines', []):
                    return True
                if record['content_words'] in exclude_filters.get('content_words', []):
                    return True
                
                # Проверяем фильтр по группам уникальных ответов
                if exclude_filters.get('response_groups'):
                    import hashlib
                    # Создаем отпечаток ответа для группировки
                    response_fingerprint = f"{record['status_code']}:{record['content_length']}:{record['content_lines']}:{record['content_words']}"
                    if record['redirect']:
                        response_fingerprint += ":redirect"
                    
                    # Создаем хеш для уникальной идентификации группы
                    fingerprint_hash = hashlib.md5(response_fingerprint.encode()).hexdigest()[:8]
                    
                    if fingerprint_hash in exclude_filters.get('response_groups', []):
                        return True
                
                return False
            
            # Фильтруем каждую категорию
            for record in all_files:
                if not should_exclude_record(record):
                    filtered_files.append(record)
            
            for record in all_directories:
                if not should_exclude_record(record):
                    filtered_directories.append(record)
            
            for record in all_routes:
                if not should_exclude_record(record):
                    filtered_routes.append(record)
            
            excluded_count = (len(all_files) - len(filtered_files) + 
                            len(all_directories) - len(filtered_directories) + 
                            len(all_routes) - len(filtered_routes))
            
            print(f"После фильтрации: файлы={len(filtered_files)}, каталоги={len(filtered_directories)}, маршруты={len(filtered_routes)}")
            print(f"Исключено записей: {excluded_count}")
            
            return filtered_files, filtered_directories, filtered_routes
        else:
            # Если фильтры не заданы, возвращаем все записи
            return all_files, all_directories, all_routes
    
    except Exception as e:
        print(f"Ошибка при парсинге CSV текста: {e}")
        return [], [], []
    
    return [], [], []

def classify_as_file(fuzz_value):
    """Определяет, является ли запись файлом"""
    # Файлы с расширениями
    file_extensions = [
        '.php', '.html', '.htm', '.js', '.css', '.txt', '.xml', '.json',
        '.ico', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.doc',
        '.docx', '.xls', '.xlsx', '.zip', '.rar', '.tar', '.gz', '.sql',
        '.log', '.conf', '.config', '.ini', '.htaccess', '.htpasswd',
        '.hta', '.profile', '.pgsql', '.mysql'
    ]
    
    fuzz_lower = fuzz_value.lower()
    return any(fuzz_lower.endswith(ext) for ext in file_extensions)

def classify_as_directory(display_name, redirect_location):
    """Определяет, является ли запись каталогом"""
    # Если есть редирект с добавлением слеша в конце - это каталог
    if redirect_location:
        # Проверяем различные варианты редиректов
        if (redirect_location.endswith(f'/{display_name}/') or 
            redirect_location.endswith(f'{display_name}/') or
            redirect_location.endswith(f'/{display_name.split("/")[-1]}/') or
            redirect_location.endswith(f'{display_name.split("/")[-1]}/')):
            return True
    
    # Известные каталоги - проверяем последнюю часть пути
    directory_names = [
        'files', 'img', 'images', 'includes', 'languages', 'misc',
        'modules', 'profiles', 'scripts', 'sites', 'themes', 'uploads',
        'assets', 'static', 'css', 'js', 'fonts', 'media', 'admin',
        'administrator', 'wp-admin', 'wp-content', 'wp-includes'
    ]
    
    # Проверяем как полный путь, так и последнюю часть
    path_parts = display_name.split('/')
    last_part = path_parts[-1] if path_parts else display_name
    
    return (display_name.lower() in directory_names or 
            last_part.lower() in directory_names)

db = SQLAlchemy(app)

# Функции для анализа Attack Surface
def resolve_domain_to_ip(domain):
    """Определяет IP-адрес для домена"""
    try:
        print(f"DEBUG: Резолвинг домена {domain}")
        
        # Извлекаем чистое доменное имя из URL
        if domain.startswith(('http://', 'https://')):
            parsed = urlparse(domain)
            clean_domain = parsed.netloc
            if ':' in clean_domain:
                clean_domain = clean_domain.split(':', 1)[0]
        else:
            clean_domain = domain
        
        print(f"DEBUG: Чистое доменное имя: {clean_domain}")
        
        if not clean_domain:
            print(f"DEBUG: Не удалось извлечь доменное имя из {domain}")
            return None
        
        ip = socket.gethostbyname(clean_domain)
        print(f"DEBUG: Домен {clean_domain} резолвится в {ip}")
        return ip
    except socket.gaierror as e:
        print(f"DEBUG: Ошибка резолвинга домена {domain}: {e}")
        return None

def detect_cidr_from_ips(ips):
    """Определяет CIDR-блоки из списка IP-адресов"""
    print(f"DEBUG: Определение CIDR для IP-адресов: {ips}")
    if not ips:
        print("DEBUG: Список IP-адресов пуст")
        return []
    
    cidrs = set()
    for ip in ips:
        try:
            print(f"DEBUG: Обработка IP {ip}")
            # Создаем сеть /24 для каждого IP
            network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            cidr_str = str(network)
            print(f"DEBUG: IP {ip} -> CIDR {cidr_str}")
            cidrs.add(cidr_str)
        except ipaddress.AddressValueError as e:
            print(f"DEBUG: Ошибка обработки IP {ip}: {e}")
            continue
    
    result = list(cidrs)
    print(f"DEBUG: Найденные CIDR блоки: {result}")
    return result

def build_attack_surface_graph(domains_and_ips):
    """Строит граф attack surface"""
    print(f"DEBUG: Начало построения графа для: {domains_and_ips}")
    graph_data = {
        'nodes': [],
        'edges': [],
        'cidrs': [],
        'domains': [],
        'ips': []
    }
    
    resolved_ips = []
    
    for item in domains_and_ips:
        item = item.strip()
        if not item:
            continue
            
        # Проверяем, является ли элемент IP-адресом
        try:
            ipaddress.IPv4Address(item)
            # Это IP-адрес - добавляем как отдельный узел
            graph_data['ips'].append(item)
            resolved_ips.append(item)
            graph_data['nodes'].append({
                'id': item,
                'label': item,
                'type': 'ip',
                'group': 'ip'
            })
        except ipaddress.AddressValueError:
            # Это домен или URL/HTTP IP с портом
            parsed = urlparse(item)
            if parsed.scheme in ('http','https') and parsed.netloc:
                host = parsed.netloc.split(':',1)[0]
                try:
                    ipaddress.IPv4Address(host)
                    # Прямой IP из URL — добавляем как IP
                    graph_data['ips'].append(host)
                    resolved_ips.append(host)
                    graph_data['nodes'].append({
                        'id': host,
                        'label': host,
                        'type': 'ip',
                        'group': 'ip'
                    })
                    continue
                except ipaddress.AddressValueError:
                    item_domain = host
            else:
                item_domain = item
            ip = resolve_domain_to_ip(item_domain)
            graph_data['domains'].append({
                'domain': item_domain,
                'ip': ip
            })
            
            # Добавляем узел домена с информацией об IP
            node_label = item_domain
            if ip:
                node_label = f"{item_domain}\n({ip})"
                resolved_ips.append(ip)
            
            graph_data['nodes'].append({
                'id': item_domain,
                'label': node_label,
                'type': 'domain',
                'group': 'domain',
                'ip': ip  # Сохраняем IP как свойство узла
            })
            
            # НЕ создаем отдельный узел для IP-адреса домена
            # НЕ создаем связь домен -> IP
    
    # Определяем CIDR-блоки
    cidrs = detect_cidr_from_ips(resolved_ips)
    graph_data['cidrs'] = cidrs
    
    # Добавляем узлы CIDR и связи
    for cidr in cidrs:
        graph_data['nodes'].append({
            'id': cidr,
            'label': cidr,
            'type': 'cidr',
            'group': 'cidr'
        })
        
        # Связываем CIDR с доменами и IP-адресами
        network = ipaddress.IPv4Network(cidr)
        
        # Связываем с узлами, которые содержат IP из этого CIDR
        for node in graph_data['nodes']:
            if node['type'] == 'ip':
                # Отдельный IP-узел (введен пользователем напрямую)
                try:
                    if ipaddress.IPv4Address(node['id']) in network:
                        graph_data['edges'].append({
                            'from': cidr,
                            'to': node['id'],
                            'label': 'contains'
                        })
                except ipaddress.AddressValueError:
                    continue
            elif node['type'] == 'domain' and node.get('ip'):
                # Доменный узел с IP-адресом
                try:
                    if ipaddress.IPv4Address(node['ip']) in network:
                        graph_data['edges'].append({
                            'from': cidr,
                            'to': node['id'],
                            'label': 'contains'
                        })
                except ipaddress.AddressValueError:
                     continue
    
    # Добавляем связи между доменами в одном CIDR блоке
    for cidr in cidrs:
        network = ipaddress.IPv4Network(cidr)
        domains_in_cidr = []
        
        # Находим все домены в этом CIDR
        for node in graph_data['nodes']:
            if node['type'] == 'domain' and node.get('ip'):
                try:
                    if ipaddress.IPv4Address(node['ip']) in network:
                        domains_in_cidr.append(node['id'])
                except ipaddress.AddressValueError:
                    continue
        
        # Создаем связи между доменами в одном CIDR (если их больше одного)
        if len(domains_in_cidr) > 1:
            for i, domain1 in enumerate(domains_in_cidr):
                for domain2 in domains_in_cidr[i+1:]:
                    graph_data['edges'].append({
                        'from': domain1,
                        'to': domain2,
                        'label': 'same_cidr',
                        'dashes': True  # Пунктирная линия для обозначения группировки
                    })
    
    return graph_data

def build_and_save_attack_surface(attack_surface_id, domains_and_ips, clear_existing=False, domain_ip_map=None):
    """Построение и сохранение Attack Surface в базе данных с иерархической структурой"""
    if clear_existing:
        CIDRBlock.query.filter_by(attack_surface_id=attack_surface_id).delete()
        IPAddress.query.filter_by(attack_surface_id=attack_surface_id).delete()
        Domain.query.filter_by(attack_surface_id=attack_surface_id).delete()
        AttackSurfacePort.query.filter_by(attack_surface_id=attack_surface_id).delete()
    
    # Словари для хранения данных
    domain_to_ip = {}
    ip_to_domains = {}
    all_ips = []
    
    # Обрабатываем каждый элемент
    provided_map = domain_ip_map or {}
    provided_map_norm = {}
    try:
        for k, v in provided_map.items():
            key = str(k).strip()
            try:
                if key.startswith(('http://','https://')):
                    u = urlparse(key)
                    key = u.netloc.split(':',1)[0]
            except Exception:
                pass
            key_l = key.lower()
            if key_l.startswith('www.'):
                key_l = key_l[4:]
            provided_map_norm[key_l] = v
    except Exception:
        provided_map_norm = provided_map
    for item in domains_and_ips:
        item = item.strip()
        if not item:
            continue
            
        # Проверяем, является ли элемент IP-адресом
        try:
            ipaddress.ip_address(item)
            if item not in all_ips:
                all_ips.append(item)
        except ValueError:
            try:
                parsed = urlparse(item)
                if parsed.scheme in ('http','https') and parsed.netloc:
                    host = parsed.netloc.split(':',1)[0]
                    try:
                        ipaddress.ip_address(host)
                        if host not in all_ips:
                            all_ips.append(host)
                        domain_to_ip[item] = host
                        if host not in ip_to_domains:
                            ip_to_domains[host] = []
                        ip_to_domains[host].append(item)
                        continue
                    except ValueError:
                        item_normalized = item
                else:
                    item_normalized = item
                host_for_resolve = urlparse(item_normalized).netloc.split(':',1)[0] if item_normalized.startswith(('http://','https://')) else item_normalized
                ip = None
                host_k = host_for_resolve.lower()
                if host_k.startswith('www.'):
                    host_k = host_k[4:]
                item_k = item_normalized.lower()
                if item_k.startswith('www.'):
                    item_k = item_k[4:]
                if host_k in provided_map_norm:
                    ip = provided_map_norm.get(host_k)
                elif item_k in provided_map_norm:
                    ip = provided_map_norm.get(item_k)
                if not ip:
                    ip = resolve_domain_to_ip(host_for_resolve)
                if ip:
                    domain_to_ip[item_normalized] = ip
                    if ip not in ip_to_domains:
                        ip_to_domains[ip] = []
                    ip_to_domains[ip].append(item_normalized)
                    if ip not in all_ips:
                        all_ips.append(ip)
                else:
                    domain_record = Domain(
                        domain=item_normalized,
                        attack_surface_id=attack_surface_id
                    )
                    db.session.add(domain_record)
            except Exception as e:
                print(f"Не удалось обработать элемент {item}: {e}")
                # Сохраняем как URL если указан протокол, иначе как домен
                domain_record = Domain(
                    domain=item if str(item).strip().lower().startswith(('http://','https://')) else str(item).strip(),
                    attack_surface_id=attack_surface_id
                )
                db.session.add(domain_record)
    
    # Определяем CIDR блоки
    cidr_blocks = detect_cidr_from_ips(all_ips)
    
    # Сохраняем CIDR блоки (без дублей)
    cidr_records = {}
    existing_cidrs = {cb.cidr: cb for cb in CIDRBlock.query.filter_by(attack_surface_id=attack_surface_id).all()}
    for cidr in cidr_blocks:
        if cidr in existing_cidrs:
            cidr_records[cidr] = existing_cidrs[cidr]
        else:
            cidr_record = CIDRBlock(
                cidr=cidr,
                attack_surface_id=attack_surface_id
            )
            db.session.add(cidr_record)
            db.session.flush()
            cidr_records[cidr] = cidr_record
    
    # Сохраняем IP-адреса с привязкой к CIDR (без дублей)
    ip_records = {rec.ip: rec for rec in IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()}
    for ip in all_ips:
        cidr_block_id = None
        for cidr, cidr_record in cidr_records.items():
            try:
                cidr_network = ipaddress.ip_network(cidr, strict=False)
                ip_addr = ipaddress.ip_address(ip)
                if ip_addr in cidr_network:
                    cidr_block_id = cidr_record.id
                    break
            except ValueError:
                continue
        if ip in ip_records:
            rec = ip_records[ip]
            changed = False
            if rec.cidr_block_id != cidr_block_id:
                rec.cidr_block_id = cidr_block_id
                changed = True
            if changed:
                db.session.flush()
            continue
        ip_record = IPAddress(
            ip=ip,
            attack_surface_id=attack_surface_id,
            cidr_block_id=cidr_block_id
        )
        db.session.add(ip_record)
        db.session.flush()
        ip_records[ip] = ip_record
    
    # Сохраняем домены с привязкой к IP (без дублей)
    existing_domains = {d.domain: d for d in Domain.query.filter_by(attack_surface_id=attack_surface_id).all()}
    for domain, ip in domain_to_ip.items():
        ip_record = ip_records.get(ip)
        if domain in existing_domains:
            drec = existing_domains[domain]
            if ip_record and drec.ip_address_id != (ip_record.id if ip_record else None):
                drec.ip_address_id = ip_record.id if ip_record else None
            continue
        domain_record = Domain(
            domain=domain,
            attack_surface_id=attack_surface_id,
            ip_address_id=ip_record.id if ip_record else None
        )
        db.session.add(domain_record)
    
    db.session.commit()
    
    # Строим граф для возврата
    return load_attack_surface_graph(attack_surface_id)

def load_attack_surface_graph(attack_surface_id):
    """Загрузка графа Attack Surface из базы данных"""
    nodes = []
    edges = []
    
    # Загружаем CIDR блоки
    cidr_blocks = CIDRBlock.query.filter_by(attack_surface_id=attack_surface_id).all()
    existing_cidrs = set()
    for cidr in cidr_blocks:
        nodes.append({
            'id': cidr.cidr,
            'label': cidr.cidr,
            'type': 'cidr',
            'group': 'cidr',
            'asn': cidr.asn,
            'organization': cidr.organization,
            'network_name': cidr.network_name
        })
        existing_cidrs.add(cidr.cidr)
    
    # Загружаем IP-адреса
    ip_addresses = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
    derived_cidrs = set()
    for ip in ip_addresses:
        # Базовый label только с IP-адресом
        label = ip.ip
        
        # Загружаем порты для IP-адреса
        ports = AttackSurfacePort.query.filter_by(
            attack_surface_id=attack_surface_id,
            ip_address_id=ip.id
        ).all()
        
        # Полный список портов (номер/протокол) без усечений
        ports_text = ''
        if ports:
            port_proto_set = set()
            for p in ports:
                try:
                    num = int(p.port)
                except Exception:
                    continue
                proto = (p.protocol or 'tcp').lower()
                port_proto_set.add((num, proto))
            dedup_ports = sorted(port_proto_set, key=lambda x: (x[0], x[1]))
            ports_text = ', '.join([f"{num}/{proto}" for (num, proto) in dedup_ports])
        
        nodes.append({
            'id': ip.ip,
            'label': label,
            'type': 'ip',
            'group': 'ip',
            # Дополнительное поле для отдельного рендера портов на фронтенде
            'ports_text': ports_text
        })
        
        # Связь CIDR -> IP
        if ip.cidr_block:
            edges.append({
                'from': ip.cidr_block.cidr,
                'to': ip.ip,
                'label': 'contains'
            })
        else:
            try:
                net = ipaddress.IPv4Network(f"{ip.ip}/24", strict=False)
                cidr_str = str(net)
                if cidr_str not in existing_cidrs and cidr_str not in derived_cidrs:
                    nodes.append({
                        'id': cidr_str,
                        'label': cidr_str,
                        'type': 'cidr',
                        'group': 'cidr',
                        'asn': None,
                        'organization': None,
                        'network_name': None
                    })
                    derived_cidrs.add(cidr_str)
                edges.append({
                    'from': cidr_str,
                    'to': ip.ip,
                    'label': 'contains'
                })
            except Exception:
                pass
    
    # Загружаем домены
    domains = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
    for domain in domains:
        # Формируем label только с именем домена (без IP)
        label = domain.domain
        
        nodes.append({
            'id': domain.domain,
            'label': label,
            'type': 'domain',
            'group': 'domain' if domain.ip_address else 'domain_unresolved',
            'ip': domain.ip_address.ip if domain.ip_address else None
        })
        
        # Связь IP -> Domain
        if domain.ip_address:
            edges.append({
                'from': domain.ip_address.ip,
                'to': domain.domain,
                'label': 'resolves_to'
            })
    
    # Связи между доменами удалены для упрощения графа
    
    return {
        'nodes': nodes,
        'edges': edges
    }

def propagate_ports_to_same_ip(attack_surface_id, domain_id, ports_data):
    """Сохранение портов на уровне IP для всех доменов с тем же IP"""
    # Получаем домен и его IP
    domain = Domain.query.get(domain_id)
    if not domain or not domain.ip_address_id:
        return

    ip_address_id = domain.ip_address_id

    # Вносим/обновляем запись портов на уровне IP (без привязки к домену)
    for port_data in ports_data:
        port_num = port_data.get('port')
        if port_num is None:
            continue
        protocol = (port_data.get('protocol') or 'tcp').lower()
        existing_port = AttackSurfacePort.query.filter_by(
            attack_surface_id=attack_surface_id,
            ip_address_id=ip_address_id,
            port=port_num,
            protocol=protocol
        ).first()

        svc = port_data.get('service', '')
        status = port_data.get('status', 'open')

        if existing_port:
            # Обновляем сервис, если ранее был пуст
            if not existing_port.service and svc:
                existing_port.service = svc
            existing_port.status = status
        else:
            new_port = AttackSurfacePort(
                port=port_num,
                service=svc,
                status=status,
                protocol=protocol,
                attack_surface_id=attack_surface_id,
                ip_address_id=ip_address_id,
                domain_id=None
            )
            db.session.add(new_port)

    db.session.commit()

# API для Attack Surface
@app.route('/api/projects/<int:project_id>/attack-surfaces', methods=['GET', 'POST'])
def api_attack_surfaces(project_id):
    """Получить все Attack Surface проекта или создать новый"""
    project = Project.query.get_or_404(project_id)
    
    if request.method == 'GET':
        attack_surfaces = AttackSurface.query.filter_by(project_id=project_id).all()
        return jsonify({
            'success': True,
            'attack_surfaces': [{
                'id': as_.id,
                'name': as_.name,
                'description': as_.description,
                'created_at': as_.created_at.isoformat(),
                'updated_at': as_.updated_at.isoformat()
            } for as_ in attack_surfaces]
        })
    
    elif request.method == 'POST':
        try:
            data = request.get_json()
            if not data or 'name' not in data:
                return jsonify({'error': 'Не указано имя Attack Surface'}), 400
            
            attack_surface = AttackSurface(
                name=data['name'],
                description=data.get('description', ''),
                project_id=project_id
            )
            
            db.session.add(attack_surface)
            db.session.commit()
            
            return jsonify({
                'success': True,
                'attack_surface': {
                    'id': attack_surface.id,
                    'name': attack_surface.name,
                    'description': attack_surface.description,
                    'created_at': attack_surface.created_at.isoformat(),
                    'updated_at': attack_surface.updated_at.isoformat()
                }
            })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка создания Attack Surface: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>', methods=['GET', 'PUT', 'DELETE'])
def api_attack_surface_detail(attack_surface_id):
    """Получить, обновить или удалить Attack Surface"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    if request.method == 'GET':
        return jsonify({
            'success': True,
            'attack_surface': {
                'id': attack_surface.id,
                'name': attack_surface.name,
                'description': attack_surface.description,
                'project_id': attack_surface.project_id,
                'created_at': attack_surface.created_at.isoformat(),
                'updated_at': attack_surface.updated_at.isoformat()
            }
        })
    
    elif request.method == 'PUT':
        try:
            data = request.get_json()
            if 'name' in data:
                attack_surface.name = data['name']
            if 'description' in data:
                attack_surface.description = data['description']
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'attack_surface': {
                    'id': attack_surface.id,
                    'name': attack_surface.name,
                    'description': attack_surface.description,
                    'updated_at': attack_surface.updated_at.isoformat()
                }
            })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка обновления Attack Surface: {str(e)}'}), 500
    
    elif request.method == 'DELETE':
        try:
            try:
                domains = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
                for d in domains:
                    delete_screenshot_file(d.screenshot)
                ip_records = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
                for ipr in ip_records:
                    delete_screenshot_file(ipr.screenshot)
            except Exception:
                pass
            db.session.delete(attack_surface)
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Attack Surface успешно удален'
            })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка удаления Attack Surface: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/analyze', methods=['POST'])
def analyze_attack_surface(attack_surface_id):
    """Анализ и сохранение данных Attack Surface"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    try:
        data = request.get_json()
        if not data or 'domains_and_ips' not in data:
            return jsonify({'error': 'Не указан список доменов и IP-адресов'}), 400
        
        domains_and_ips = data['domains_and_ips']
        if isinstance(domains_and_ips, str):
            domains_and_ips = [item.strip() for item in domains_and_ips.split('\n') if item.strip()]
        
        clear_existing = bool(data.get('clear_existing', False))
        domain_ip_map = data.get('domain_ip_map') or {}
        if clear_existing:
            AttackSurfaceTechnology.query.filter_by(attack_surface_id=attack_surface_id).delete()
        graph_data = build_and_save_attack_surface(attack_surface_id, domains_and_ips, clear_existing=clear_existing, domain_ip_map=domain_ip_map)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'graph': graph_data
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка анализа: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/graph', methods=['GET'])
def get_attack_surface_graph(attack_surface_id):
    """Получить граф Attack Surface из базы данных"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    try:
        graph_data = load_attack_surface_graph(attack_surface_id)
        
        return jsonify({
            'success': True,
            'graph': graph_data
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка загрузки графа: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/ports-summary', methods=['GET'])
def attack_surface_ports_summary(attack_surface_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    try:
        selected_cidrs = request.args.getlist('cidr')
        ip_records = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
        relevant_ip_ids = []
        if selected_cidrs:
            networks = []
            for cidr in selected_cidrs:
                try:
                    networks.append(ipaddress.ip_network(cidr, strict=False))
                except ValueError:
                    continue
            for ip in ip_records:
                try:
                    ip_addr = ipaddress.ip_address(ip.ip)
                except ValueError:
                    continue
                for net in networks:
                    if ip_addr in net:
                        relevant_ip_ids.append(ip.id)
                        break
        else:
            relevant_ip_ids = [ip.id for ip in ip_records]

        if not relevant_ip_ids:
            summary = []
            web_ports_list = []
        else:
            counts_query = db.session.query(
                AttackSurfacePort.port,
                AttackSurfacePort.protocol,
                func.count(func.distinct(IPAddress.id))
            )\
                .join(IPAddress, AttackSurfacePort.ip_address_id == IPAddress.id)\
                .filter(
                    AttackSurfacePort.attack_surface_id == attack_surface_id,
                    AttackSurfacePort.status == 'open',
                    AttackSurfacePort.ip_address_id.in_(relevant_ip_ids)
                )\
                .group_by(AttackSurfacePort.port, AttackSurfacePort.protocol)
            results = counts_query.all()
            by_port = {}
            for p, proto, cnt in results:
                if p is None:
                    continue
                try:
                    port_num = int(p)
                except Exception:
                    continue
                proto_key = (proto or 'tcp').lower()
                if port_num not in by_port:
                    by_port[port_num] = {'tcp': 0, 'udp': 0}
                if proto_key in by_port[port_num]:
                    by_port[port_num][proto_key] += int(cnt)
            summary_items = []
            for port_num, counts in by_port.items():
                total = int(counts.get('tcp', 0)) + int(counts.get('udp', 0))
                summary_items.append({'port': port_num, 'counts': {'tcp': int(counts.get('tcp', 0)), 'udp': int(counts.get('udp', 0))}, 'total': total})
            summary = sorted(summary_items, key=lambda x: x['total'], reverse=True)

            web_ports_query = db.session.query(AttackSurfacePort.port)\
                .filter(
                    AttackSurfacePort.attack_surface_id == attack_surface_id,
                    AttackSurfacePort.ip_address_id.in_(relevant_ip_ids),
                    AttackSurfacePort.protocol == 'tcp',
                    AttackSurfacePort.is_web == True
                )\
                .distinct()
            web_ports_list = [int(row[0]) for row in web_ports_query.all() if row and row[0] is not None]
        cidrs_query = db.session.query(CIDRBlock.cidr)\
            .filter(CIDRBlock.attack_surface_id == attack_surface_id)\
            .distinct().all()
        cidr_set = set(row[0] for row in cidrs_query)
        # Добавляем фолбек CIDR (/24) для IP-адресов без сохраненной привязки
        for ip in ip_records:
            try:
                net = ipaddress.ip_network(f"{ip.ip}/24", strict=False)
                cidr_set.add(str(net))
            except ValueError:
                continue
        cidr_list = sorted(cidr_set)
        return jsonify({'success': True, 'ports': summary, 'cidrs': cidr_list, 'web_ports': web_ports_list})
    except Exception as e:
        return jsonify({'error': f'Ошибка получения сводки портов: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/ip-addresses', methods=['GET'])
def get_attack_surface_ip_addresses(attack_surface_id):
    try:
        ip_records = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
        return jsonify({'success': True, 'ips': [{'id': ip.id, 'ip': ip.ip, 'screenshot': ip.screenshot, 'screenshot_status_code': ip.screenshot_status_code, 'screenshot_url': ip.screenshot_url, 'screenshot_checked_at': ip.screenshot_checked_at.isoformat() if ip.screenshot_checked_at else None} for ip in ip_records]})
    except Exception as e:
        return jsonify({'error': f'Ошибка получения IP: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/ip-addresses/<int:ip_id>', methods=['DELETE'])
def delete_attack_surface_ip(attack_surface_id, ip_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    ip_obj = IPAddress.query.filter_by(id=ip_id, attack_surface_id=attack_surface_id).first_or_404()
    try:
        try:
            delete_screenshot_file(ip_obj.screenshot)
        except Exception:
            pass
        domains = Domain.query.filter_by(attack_surface_id=attack_surface_id, ip_address_id=ip_id).all()
        for d in domains:
            try:
                delete_screenshot_file(d.screenshot)
            except Exception:
                pass
            AttackSurfacePort.query.filter_by(attack_surface_id=attack_surface_id, domain_id=d.id).delete()
            AttackSurfaceTechnology.query.filter_by(attack_surface_id=attack_surface_id, domain_id=d.id).delete()
            db.session.delete(d)
        AttackSurfacePort.query.filter_by(attack_surface_id=attack_surface_id, ip_address_id=ip_id).delete()
        db.session.delete(ip_obj)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка удаления IP: {str(e)}'}), 500

@app.route('/api/cidr/asn-info', methods=['GET'])
def cidr_asn_info():
    cidr_input = request.args.get('cidr', '').strip()
    attack_surface_id = request.args.get('attack_surface_id', type=int)
    if not cidr_input:
        return jsonify({'error': 'Не указан CIDR'}), 400
    try:
        network = ipaddress.ip_network(cidr_input, strict=False)
        lookup_ip = None
        # Выбираем любой IP из базы, попадающий в CIDR
        try:
            if attack_surface_id is not None:
                ip_records = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
            else:
                ip_records = IPAddress.query.all()
            for rec in ip_records:
                try:
                    ip_addr = ipaddress.ip_address(rec.ip)
                except ValueError:
                    continue
                if ip_addr in network:
                    lookup_ip = str(ip_addr)
                    break
        except Exception:
            lookup_ip = None
        # Фолбек: первый хост сети, либо network address
        if not lookup_ip:
            hosts_iter = network.hosts()
            try:
                first_host = next(hosts_iter)
                lookup_ip = str(first_host)
            except StopIteration:
                lookup_ip = str(network.network_address)
    except ValueError:
        try:
            ip = ipaddress.ip_address(cidr_input)
            lookup_ip = str(ip)
            network = ipaddress.ip_network(f"{ip}/32", strict=False)
        except ValueError:
            return jsonify({'error': 'Некорректный CIDR или IP'}), 400

    result = {
        'success': False,
        'cidr': str(network),
        'ip': lookup_ip,
        'asn': None,
        'organization': None,
        'rir': None,
        'prefix': None,
        'source': None,
        'network_name': None
    }

    # Попытка получить данные через локальный ipinfo CLI
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        ipinfo_bin = os.path.join(base_dir, 'ipinfo')
        if os.path.exists(ipinfo_bin) and os.access(ipinfo_bin, os.X_OK):
            token = os.environ.get('IPINFO_TOKEN')
            cmd = [ipinfo_bin, lookup_ip, '--json']
            if token:
                cmd.extend(['--token', token])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            output = (proc.stdout or '').strip()
            if output:
                info = json.loads(output)
                org = info.get('org') or ''
                company = info.get('company') or {}
                hostname = info.get('hostname') or ''
                asn_info = info.get('asn') or {}
                asn_val = None
                org_name = None
                if isinstance(org, str) and org:
                    parts = org.split(' ', 1)
                    if parts and parts[0].upper().startswith('AS'):
                        asn_val = parts[0]
                        if len(parts) > 1:
                            org_name = parts[1]
                if not asn_val and isinstance(asn_info, dict):
                    num = asn_info.get('asn') or asn_info.get('asn_number')
                    if num:
                        asn_val = f"AS{num}" if str(num).isdigit() else str(num)
                if not org_name and isinstance(company, dict):
                    org_name = company.get('name')
                result['asn'] = asn_val
                result['organization'] = org_name or org or hostname
                result['network_name'] = hostname or (org_name if org_name else None)
                result['source'] = 'ipinfo'
                result['prefix'] = str(network)
                result['success'] = True
    except Exception:
        pass

    if not result['success']:
        try:
            existing = CIDRBlock.query.filter_by(cidr=str(network)).first()
            if existing and (existing.asn or existing.organization or existing.network_name):
                result['asn'] = existing.asn
                result['organization'] = existing.organization
                result['network_name'] = existing.network_name
                result['source'] = 'db'
                result['prefix'] = str(network)
                result['success'] = True
        except Exception:
            pass
    if not result['success']:
        try:
            token = os.environ.get('IPINFO_TOKEN')
            url = f"https://ipinfo.io/{lookup_ip}/json"
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads((resp.read() or b'{}').decode('utf-8'))
                org = data.get('org') or ''
                company = data.get('company') or {}
                hostname = data.get('hostname') or ''
                asn_info = data.get('asn') or {}
                asn_val = None
                org_name = None
                if isinstance(org, str) and org:
                    parts = org.split(' ', 1)
                    if parts and parts[0].upper().startswith('AS'):
                        asn_val = parts[0]
                        if len(parts) > 1:
                            org_name = parts[1]
                if not asn_val and isinstance(asn_info, dict):
                    num = asn_info.get('asn') or asn_info.get('asn_number')
                    if num:
                        asn_val = f"AS{num}" if str(num).isdigit() else str(num)
                if not org_name and isinstance(company, dict):
                    org_name = company.get('name')
                result['asn'] = asn_val
                result['organization'] = org_name or org or hostname
                result['network_name'] = hostname or (org_name if org_name else None)
                result['source'] = 'ipinfo_http'
                result['prefix'] = str(network)
                result['success'] = True
                try:
                    existing = CIDRBlock.query.filter_by(cidr=str(network)).first()
                    if existing:
                        existing.asn = asn_val
                        existing.organization = org_name or org or hostname
                        existing.network_name = result['network_name']
                        db.session.commit()
                except Exception:
                    db.session.rollback()
        except Exception:
            pass
    if not result['success']:
        result['prefix'] = str(network)
        result['source'] = 'basic'
        result['success'] = True
    return jsonify(result)

@app.route('/api/projects/<int:project_id>/ports-summary', methods=['GET'])
def project_ports_summary(project_id):
    project = Project.query.get_or_404(project_id)
    try:
        selected_cidrs = request.args.getlist('cidr')
        counts_query = db.session.query(AttackSurfacePort.port, func.count(AttackSurfacePort.id))\
            .join(AttackSurface, AttackSurfacePort.attack_surface_id == AttackSurface.id)\
            .filter(AttackSurface.project_id == project_id)
        if selected_cidrs:
            counts_query = counts_query.join(IPAddress, AttackSurfacePort.ip_address_id == IPAddress.id)\
                .join(CIDRBlock, IPAddress.cidr_block_id == CIDRBlock.id)\
                .filter(CIDRBlock.cidr.in_(selected_cidrs))
        counts_query = counts_query.group_by(AttackSurfacePort.port).order_by(func.count(AttackSurfacePort.id).desc())
        results = counts_query.all()
        summary = [{'port': int(p), 'count': int(c)} for (p, c) in results if p is not None]
        cidrs_query = db.session.query(CIDRBlock.cidr)\
            .join(AttackSurface, CIDRBlock.attack_surface_id == AttackSurface.id)\
            .filter(AttackSurface.project_id == project_id)\
            .distinct().all()
        cidr_list = [row[0] for row in cidrs_query]
        return jsonify({'success': True, 'ports': summary, 'cidrs': cidr_list})
    except Exception as e:
        return jsonify({'error': f'Ошибка получения сводки портов: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/domains-with-sites', methods=['GET'])
def get_domains_with_sites(attack_surface_id):
    """Получить список доменов с существующими карточками сайтов"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    try:
        # Получаем все домены из Attack Surface
        domains = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
        
        # Получаем все сайты и извлекаем домены из URL
        websites = Website.query.all()
        website_domains = set()
        
        for website in websites:
            try:
                parsed_url = urlparse(website.url)
                domain = parsed_url.netloc.lower()
                # Убираем www. если есть
                if domain.startswith('www.'):
                    domain = domain[4:]
                website_domains.add(domain)
            except:
                continue
        
        # Находим пересечения
        domains_with_sites = []
        for domain in domains:
            # Извлекаем домен из URL, если это URL
            domain_value = domain.domain.lower()
            if domain_value.startswith(('http://', 'https://')):
                try:
                    parsed_domain = urlparse(domain_value)
                    domain_name = parsed_domain.netloc.lower()
                    if domain_name.startswith('www.'):
                        domain_name = domain_name[4:]
                except:
                    domain_name = domain_value
            else:
                domain_name = domain_value
            
            # Проверяем как с www, так и без
            if (domain_name in website_domains or 
                f'www.{domain_name}' in website_domains or
                domain_name.replace('www.', '') in website_domains):
                domains_with_sites.append(domain_name)
        
        return jsonify({
            'success': True,
            'domains_with_sites': domains_with_sites
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка получения доменов: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/httpx-check', methods=['GET'])
def httpx_check_port(attack_surface_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    try:
        ip = (request.args.get('ip') or '').strip()
        port = request.args.get('port')
        try:
            port = int(port)
        except Exception:
            return jsonify({'error': 'Некорректный порт'}), 400
        if not ip:
            return jsonify({'error': 'Не указан IP'}), 400

        ip_record = IPAddress.query.filter_by(attack_surface_id=attack_surface_id, ip=ip).first()
        if not ip_record:
            return jsonify({'error': 'IP не входит в Attack Surface'}), 400

        result = {
            'success': True,
            'ip': ip,
            'port': port,
            'web': False,
            'scheme': None,
            'status_code': None,
            'final_url': None
        }

        def try_open(url):
            try:
                req = urllib.request.Request(url, method='GET')
                ctx = None
                if url.lower().startswith('https://'):
                    try:
                        ctx = ssl._create_unverified_context()
                    except Exception:
                        ctx = None
                with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                    code = getattr(resp, 'status', None) or 200
                    final_url = getattr(resp, 'url', url)
                    ct = (resp.headers.get('Content-Type') or '').lower()
                    return True, code, final_url, ct
            except urllib.error.HTTPError as e:
                try:
                    code = getattr(e, 'code', None) or 400
                    final_url = getattr(e, 'url', url)
                    headers = getattr(e, 'headers', None)
                    ct = (headers.get('Content-Type') if headers else '')
                    ct = (ct or '').lower()
                    return True, code, final_url, ct
                except Exception:
                    return False, None, None, None
            except Exception:
                return False, None, None, None

        ok, code, final, ct = try_open(f'http://{ip}:{port}/')
        if ok:
            result.update({'web': True, 'scheme': 'http', 'status_code': code, 'final_url': final})
        else:
            ok2, code2, final2, ct2 = try_open(f'https://{ip}:{port}/')
            if ok2:
                result.update({'web': True, 'scheme': 'https', 'status_code': code2, 'final_url': final2})

        try:
            port_row = AttackSurfacePort.query.filter_by(
                attack_surface_id=attack_surface_id,
                ip_address_id=ip_record.id,
                port=port,
                protocol='tcp'
            ).first()
            if port_row:
                port_row.is_web = bool(result['web'])
                port_row.web_scheme = result['scheme']
                port_row.web_status_code = result['status_code']
                port_row.web_final_url = result['final_url']
                port_row.web_checked_at = datetime.utcnow()
                db.session.add(port_row)
                db.session.commit()
        except Exception:
            db.session.rollback()

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'Ошибка проверки: {str(e)}'}), 500

def _parse_headers_payload(payload):
    headers = {}
    if not payload:
        return headers
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        s = payload.strip()
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        for line in s.split('\n'):
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip()] = v.strip()
        return headers
    return headers

def _guess_url_for_domain(domain_obj):
    host_or_url = domain_obj.domain.strip()
    if host_or_url.lower().startswith(('http://', 'https://')):
        return host_or_url
    scheme = 'http'
    try:
        # Если домен сохранен как URL — возвращаем как есть
        if host_or_url.lower().startswith(('http://','https://')):
            return host_or_url
        # Иначе определяем схему по портам IP
        if domain_obj.ip_address_id:
            q = AttackSurfacePort.query.filter_by(ip_address_id=domain_obj.ip_address_id, protocol='tcp').all()
            web_ports = [p for p in q if p.is_web]
            if any((p.web_scheme == 'https') or int(p.port or 0) == 443 for p in web_ports):
                scheme = 'https'
    except Exception:
        pass
    return f"{scheme}://{host_or_url}"

def _guess_url_for_ip(ip_obj):
    s = getattr(ip_obj, 'ip', None)
    s = (s.strip() if isinstance(s, str) else (str(ip_obj).strip() if isinstance(ip_obj, str) else None))
    if not s:
        return None
    low = s.lower()
    if low.startswith('http://') or low.startswith('https://'):
        return s
    scheme = 'http'
    port = None
    try:
        q = AttackSurfacePort.query.filter_by(ip_address_id=getattr(ip_obj, 'id', None), protocol='tcp').all()
        web_ports = [p for p in q if p.is_web]
        if web_ports:
            wp = None
            for p in web_ports:
                if int(p.port or 0) == 443:
                    wp = p
                    break
            if not wp:
                wp = web_ports[0]
            scheme = (wp.web_scheme or ('https' if int(wp.port or 0) == 443 else 'http'))
            try:
                port = int(wp.port)
            except Exception:
                port = None
    except Exception:
        pass
    if port:
        return f"{scheme}://{s}:{port}/"
    return f"{scheme}://{s}/"

def _fetch_status(url, headers=None):
    try:
        req = urllib.request.Request(url, method='GET')
        if isinstance(headers, dict):
            for k, v in headers.items():
                req.add_header(str(k), str(v))
        ctx = None
        if url.lower().startswith('https://'):
            try:
                ctx = ssl._create_unverified_context()
            except Exception:
                ctx = None
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            code = getattr(resp, 'status', None) or 200
            final_url = getattr(resp, 'url', url)
            return True, code, final_url
    except urllib.error.HTTPError as e:
        try:
            code = getattr(e, 'code', None) or 400
            final_url = getattr(e, 'url', url)
            return True, code, final_url
        except Exception:
            return False, None, None
    except Exception:
        return False, None, None

def _fetch_status_sni(ip_url, host_header, headers=None):
    try:
        p = urlparse(ip_url)
        scheme = (p.scheme or 'https').lower()
        ip = p.hostname
        port = p.port or (443 if scheme == 'https' else 80)
        path = p.path or '/'
        if p.query:
            path = path + '?' + p.query
        headers = headers or {}
        ua = None
        for k, v in (headers.items() if isinstance(headers, dict) else []):
            if str(k).lower() == 'user-agent':
                ua = v
                break
        if not ua:
            ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            f"User-Agent: {ua}",
            "Accept: */*",
            "Connection: close",
        ]
        for k, v in (headers.items() if isinstance(headers, dict) else []):
            kl = str(k).strip()
            if kl.lower() in ('user-agent', 'host'):
                continue
            req_lines.append(f"{kl}: {v}")
        req_data = ("\r\n".join(req_lines) + "\r\n\r\n").encode('utf-8')
        import socket as _sock
        s = _sock.create_connection((ip, port), timeout=15)
        try:
            if scheme == 'https':
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                tls = ctx.wrap_socket(s, server_hostname=host_header)
            else:
                tls = s
            tls.sendall(req_data)
            buf = b''
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\r\n\r\n" in buf:
                    break
            text = buf.decode('iso-8859-1', errors='ignore')
            first = text.split('\r\n', 1)[0]
            code = None
            try:
                parts = first.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    code = int(parts[1])
            except Exception:
                pass
            loc = None
            for line in text.split('\r\n'):
                if line.lower().startswith('location:'):
                    loc = line.split(':',1)[1].strip()
                    break
            final_url = loc if loc else ip_url
            return (code is not None), code, final_url
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception:
        return False, None, None

@app.route('/api/attack-surfaces/<int:attack_surface_id>/domains/<int:domain_id>/screenshot', methods=['POST'])
def screenshot_domain(attack_surface_id, domain_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    domain_obj = Domain.query.filter_by(id=domain_id, attack_surface_id=attack_surface_id).first_or_404()
    try:
        data = request.get_json(silent=True) or {}
        headers = _parse_headers_payload(data.get('headers'))
        # Готовим заголовки и URL с учётом IP и Host
        host_name = domain_obj.domain.strip()
        try:
            if host_name.startswith(('http://','https://')):
                u = urlparse(host_name)
                host_name = (u.hostname or host_name.replace('http://','').replace('https://','').split('/')[0])
            else:
                host_name = host_name.split('/')[0]
            host_name = host_name.replace('www.', '')
        except Exception:
            pass
        headers_ex = dict(headers or {})
        if 'Host' not in {k for k in headers_ex.keys()} and host_name:
            headers_ex['Host'] = host_name
        if 'Accept-Language' not in {k for k in headers_ex.keys()}:
            headers_ex['Accept-Language'] = 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
        url = _guess_url_for_domain(domain_obj)
        ip_url = None
        try:
            if domain_obj.ip_address:
                ip_url = _guess_url_for_ip(domain_obj.ip_address)
        except Exception:
            ip_url = None
        delay_ms = int(request.args.get('delay_ms', 7000)) if request.args and request.args.get('delay_ms') else 7000
        shot = capture_screenshot_headless(url, headers=headers, delay_ms=delay_ms, project_id=attack_surface.project_id)
        code = None
        final_url = None
        if not shot:
            try:
                print('SHOT_NONE_DOMAIN', domain_obj.domain, (ip_url or url))
            except Exception:
                pass
        if shot:
            domain_obj.screenshot = shot
            domain_obj.screenshot_url = url
            try:
                mp = os.path.join(app.root_path, 'static', domain_obj.screenshot.rsplit('/',1)[0], domain_obj.screenshot.rsplit('/',1)[1].rsplit('.',1)[0] + '.json')
                if os.path.exists(mp):
                    with open(mp, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                        if meta.get('status_code') is not None:
                            code = meta.get('status_code')
                        if meta.get('final_url'):
                            final_url = meta.get('final_url')
            except Exception:
                pass
        if code is None:
            try:
                ok2, code2, final2 = _fetch_status(domain_obj.screenshot_url or url, headers)
                code = code2 if ok2 else code
            except Exception:
                pass
        domain_obj.screenshot_status_code = code
        domain_obj.screenshot_checked_at = datetime.utcnow()
        db.session.add(domain_obj)
        db.session.commit()
        return jsonify({'success': True, 'screenshot': shot, 'status_code': code, 'final_url': domain_obj.screenshot_url})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка скриншота: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/ip-addresses/<int:ip_id>/screenshot', methods=['POST'])
def screenshot_ip(attack_surface_id, ip_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    ip_obj = IPAddress.query.filter_by(id=ip_id, attack_surface_id=attack_surface_id).first_or_404()
    try:
        data = request.get_json(silent=True) or {}
        headers = _parse_headers_payload(data.get('headers'))
        url = _guess_url_for_ip(ip_obj)
        try:
            dmap = Domain.query.filter_by(attack_surface_id=attack_surface_id, ip_address_id=ip_id).first()
            if dmap:
                url = _guess_url_for_domain(dmap)
        except Exception:
            pass
        delay_ms = int(request.args.get('delay_ms', 7000)) if request.args and request.args.get('delay_ms') else 7000
        shot = capture_screenshot_headless(url, headers=headers, delay_ms=delay_ms, project_id=attack_surface.project_id)
        code = None
        final_url = None
        if not shot:
            try:
                print('SHOT_NONE_IP', ip_obj.ip, url)
            except Exception:
                pass
        if shot:
            ip_obj.screenshot = shot
            ip_obj.screenshot_url = url
            try:
                mp = os.path.join(app.root_path, 'static', ip_obj.screenshot.rsplit('/',1)[0], ip_obj.screenshot.rsplit('/',1)[1].rsplit('.',1)[0] + '.json')
                if os.path.exists(mp):
                    with open(mp, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                        if meta.get('status_code') is not None:
                            code = meta.get('status_code')
                        if meta.get('final_url'):
                            final_url = meta.get('final_url')
            except Exception:
                pass
            if final_url:
                ip_obj.screenshot_url = final_url
        if code is None:
            try:
                ok2, code2, final2 = _fetch_status(ip_obj.screenshot_url or url, headers)
                code = code2 if ok2 else code
                if ok2 and final2:
                    ip_obj.screenshot_url = final2
            except Exception:
                pass
        ip_obj.screenshot_status_code = code
        ip_obj.screenshot_checked_at = datetime.utcnow()
        db.session.add(ip_obj)
        db.session.commit()
        return jsonify({'success': True, 'screenshot': shot, 'status_code': code, 'final_url': ip_obj.screenshot_url})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка скриншота: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/screenshots', methods=['POST'])
def screenshot_all(attack_surface_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    try:
        data = request.get_json(silent=True) or {}
        headers = _parse_headers_payload(data.get('headers'))
        delay_ms = int(data.get('delay_ms', 7000))
        domains = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
        ips = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
        d_ok = 0
        d_fail = 0
        i_ok = 0
        i_fail = 0
        for d in domains:
            try:
                url = _guess_url_for_domain(d)
                ip_url = None
                try:
                    if d.ip_address:
                        ip_url = _guess_url_for_ip(d.ip_address)
                except Exception:
                    ip_url = None
                # Заголовки с Host
                host_name = d.domain.strip()
                try:
                    if host_name.startswith(('http://','https://')):
                        u = urlparse(host_name)
                        host_name = (u.hostname or host_name.replace('http://','').replace('https://','').split('/')[0])
                    else:
                        host_name = host_name.split('/')[0]
                    host_name = host_name.replace('www.', '')
                except Exception:
                    pass
                headers_ex = dict(headers or {})
                if 'Host' not in {k for k in headers_ex.keys()} and host_name:
                    headers_ex['Host'] = host_name
                if 'Accept-Language' not in {k for k in headers_ex.keys()}:
                    headers_ex['Accept-Language'] = 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
                shot = capture_screenshot_headless(url, headers=headers, delay_ms=delay_ms, project_id=attack_surface.project_id)
                code = None
                final_url = None
                if not shot:
                    try:
                        print('SHOT_NONE_DOMAIN', d.domain, (ip_url or url))
                    except Exception:
                        pass
                if shot:
                    d.screenshot = shot
                    d.screenshot_url = url
                    try:
                        mp = os.path.join(app.root_path, 'static', d.screenshot.rsplit('/',1)[0], d.screenshot.rsplit('/',1)[1].rsplit('.',1)[0] + '.json')
                        if os.path.exists(mp):
                            with open(mp, 'r', encoding='utf-8') as f:
                                meta = json.load(f)
                                if meta.get('status_code') is not None:
                                    code = meta.get('status_code')
                                if meta.get('final_url'):
                                    final_url = meta.get('final_url')
                    except Exception:
                        pass
                    if final_url:
                        pass
                if code is None:
                    try:
                        ok2, code2, final2 = _fetch_status(d.screenshot_url or url, headers)
                        code = code2 if ok2 else code
                    except Exception:
                        pass
                d.screenshot_status_code = code
                d.screenshot_checked_at = datetime.utcnow()
                db.session.add(d)
                d_ok += 1
            except Exception:
                d_fail += 1
        for ip in ips:
            try:
                url = _guess_url_for_ip(ip)
                try:
                    dmap = Domain.query.filter_by(attack_surface_id=attack_surface_id, ip_address_id=ip.id).first()
                    if dmap:
                        url = _guess_url_for_domain(dmap)
                except Exception:
                    pass
                shot = capture_screenshot_headless(url, headers=headers, delay_ms=delay_ms, project_id=attack_surface.project_id)
                code = None
                final_url = None
                if not shot:
                    try:
                        print('SHOT_NONE_IP', ip.ip, url)
                    except Exception:
                        pass
                if shot:
                    ip.screenshot = shot
                    ip.screenshot_url = url
                    try:
                        mp = os.path.join(app.root_path, 'static', ip.screenshot.rsplit('/',1)[0], ip.screenshot.rsplit('/',1)[1].rsplit('.',1)[0] + '.json')
                        if os.path.exists(mp):
                            with open(mp, 'r', encoding='utf-8') as f:
                                meta = json.load(f)
                                if meta.get('status_code') is not None:
                                    code = meta.get('status_code')
                                if meta.get('final_url'):
                                    final_url = meta.get('final_url')
                    except Exception:
                        pass
                    if final_url:
                        ip.screenshot_url = final_url
                ip.screenshot_status_code = code
                ip.screenshot_checked_at = datetime.utcnow()
                db.session.add(ip)
                i_ok += 1
            except Exception:
                i_fail += 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'success': True, 'domains_done': d_ok, 'domains_failed': d_fail, 'ips_done': i_ok, 'ips_failed': i_fail})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка массового скриншота: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/screenshots', methods=['DELETE'])
def delete_all_screenshots_attack_surface(attack_surface_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    try:
        domains = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
        ips = IPAddress.query.filter_by(attack_surface_id=attack_surface_id).all()
        d_cleared = 0
        i_cleared = 0
        for d in domains:
            try:
                delete_screenshot_file(d.screenshot)
            except Exception:
                pass
            d.screenshot = None
            d.screenshot_status_code = None
            d.screenshot_url = None
            d.screenshot_checked_at = None
            db.session.add(d)
            d_cleared += 1
        for ip in ips:
            try:
                delete_screenshot_file(ip.screenshot)
            except Exception:
                pass
            ip.screenshot = None
            ip.screenshot_status_code = None
            ip.screenshot_url = None
            ip.screenshot_checked_at = None
            db.session.add(ip)
            i_cleared += 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'success': True, 'domains_cleared': d_cleared, 'ips_cleared': i_cleared})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка очистки скриншотов: {str(e)}'}), 500

@app.route('/api/projects/<int:project_id>/screenshots', methods=['POST'])
def screenshot_project(project_id):
    project = Project.query.get_or_404(project_id)
    try:
        data = request.get_json(silent=True) or {}
        headers = _parse_headers_payload(data.get('headers'))
        only_web = bool(data.get('only_web', False))
        attack_surfaces = AttackSurface.query.filter_by(project_id=project_id).all()
        total_as = 0
        d_ok_total = 0
        d_fail_total = 0
        i_ok_total = 0
        i_fail_total = 0
        for as_ in attack_surfaces:
            total_as += 1
            domains = Domain.query.filter_by(attack_surface_id=as_.id).all()
            ips = IPAddress.query.filter_by(attack_surface_id=as_.id).all()
            for d in domains:
                try:
                    if only_web:
                        if not d.ip_address_id:
                            raise Exception('skip_non_web_no_ip')
                        q = AttackSurfacePort.query.filter_by(ip_address_id=d.ip_address_id, protocol='tcp').all()
                        if not any(p.is_web for p in q):
                            raise Exception('skip_non_web')
                    url = _guess_url_for_domain(d)
                    shot = capture_screenshot_headless(url, headers=headers, delay_ms=3000, project_id=project_id)
                    code = None
                    final_url = None
                    d.screenshot = shot
                    d.screenshot_url = url
                    try:
                        if shot:
                            mp = os.path.join(app.root_path, 'static', d.screenshot.rsplit('/',1)[0], d.screenshot.rsplit('/',1)[1].rsplit('.',1)[0] + '.json')
                            if os.path.exists(mp):
                                with open(mp, 'r', encoding='utf-8') as f:
                                    meta = json.load(f)
                                    if meta.get('status_code') is not None:
                                        code = meta.get('status_code')
                                    if meta.get('final_url'):
                                        final_url = meta.get('final_url')
                    except Exception:
                        pass
                    if final_url:
                        pass
                    d.screenshot_status_code = code
                    d.screenshot_checked_at = datetime.utcnow()
                    db.session.add(d)
                    d_ok_total += 1
                except Exception:
                    d_fail_total += 1
            for ip in ips:
                try:
                    if only_web:
                        q = AttackSurfacePort.query.filter_by(ip_address_id=ip.id, protocol='tcp').all()
                        if not any(p.is_web for p in q):
                            raise Exception('skip_non_web')
                    url = _guess_url_for_ip(ip)
                    shot = capture_screenshot_headless(url, headers=headers, delay_ms=3000, project_id=project_id)
                    code = None
                    final_url = None
                    ip.screenshot = shot
                    ip.screenshot_url = url
                    try:
                        if shot:
                            mp = os.path.join(app.root_path, 'static', ip.screenshot.rsplit('/',1)[0], ip.screenshot.rsplit('/',1)[1].rsplit('.',1)[0] + '.json')
                            if os.path.exists(mp):
                                with open(mp, 'r', encoding='utf-8') as f:
                                    meta = json.load(f)
                                    if meta.get('status_code') is not None:
                                        code = meta.get('status_code')
                                    if meta.get('final_url'):
                                        final_url = meta.get('final_url')
                    except Exception:
                        pass
                    if final_url:
                        ip.screenshot_url = final_url
                    ip.screenshot_status_code = code
                    ip.screenshot_checked_at = datetime.utcnow()
                    db.session.add(ip)
                    i_ok_total += 1
                except Exception:
                    i_fail_total += 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'success': True, 'attack_surfaces': total_as, 'domains_done': d_ok_total, 'domains_failed': d_fail_total, 'ips_done': i_ok_total, 'ips_failed': i_fail_total})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка массового скриншота проекта: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/create-site', methods=['POST'])
def create_site_from_attack_surface(attack_surface_id):
    """Создать карточку сайта из элемента Attack Surface"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    try:
        data = request.get_json()
        if not data or 'target' not in data:
            return jsonify({'error': 'Не указана цель'}), 400
        
        target = data['target']
        
        # Определяем URL для сайта
        if target.startswith('http'):
            url = target
            name = urlparse(target).netloc
        else:
            url = f"http://{target}"
            name = target
        
        # Создаем новый сайт в том же проекте
        website = Website(
            name=name,
            url=url,
            project_id=attack_surface.project_id,
            status='Не начат'
        )
        
        db.session.add(website)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'website_id': website.id,
            'message': f'Карточка сайта "{name}" успешно создана'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка создания сайта: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/domains', methods=['GET'])
def get_attack_surface_domains(attack_surface_id):
    """Получить список всех доменов Attack Surface с их ID"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    try:
        domains = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
        
        return jsonify({
            'success': True,
            'domains': [{
                'id': d.id,
                'domain': d.domain,
                'ip': d.ip_address.ip if d.ip_address else None,
                'screenshot': d.screenshot,
                'screenshot_status_code': d.screenshot_status_code,
                'screenshot_url': d.screenshot_url,
                'screenshot_checked_at': d.screenshot_checked_at.isoformat() if d.screenshot_checked_at else None
            } for d in domains]
        })

    except Exception as e:
        return jsonify({'error': f'Ошибка получения доменов: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/domains/<int:domain_id>', methods=['DELETE'])
def delete_attack_surface_domain(attack_surface_id, domain_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    domain_obj = Domain.query.filter_by(id=domain_id, attack_surface_id=attack_surface_id).first_or_404()
    try:
        try:
            delete_screenshot_file(domain_obj.screenshot)
        except Exception:
            pass
        AttackSurfacePort.query.filter_by(attack_surface_id=attack_surface_id, domain_id=domain_id).delete()
        AttackSurfaceTechnology.query.filter_by(attack_surface_id=attack_surface_id, domain_id=domain_id).delete()
        db.session.delete(domain_obj)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка удаления домена: {str(e)}'}), 500
@app.route('/api/attack-surfaces/<int:attack_surface_id>/domains/<int:domain_id>/ports', methods=['GET', 'POST'])
def domain_ports_api(attack_surface_id, domain_id):
    """Получить или сохранить порты уровня IP для домена"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    domain = Domain.query.filter_by(id=domain_id, attack_surface_id=attack_surface_id).first_or_404()
    
    if request.method == 'GET':
        # Получить порты для IP-адреса домена (IP-уровень)
        if domain.ip_address_id:
            ip_ports = AttackSurfacePort.query.filter_by(
                attack_surface_id=attack_surface_id,
                ip_address_id=domain.ip_address_id,
                status='open'
            ).all()
        else:
            ip_ports = []

        # Дедупликация по номеру порта и протоколу
        port_map = {}
        ordered_ports = []
        for p in ip_ports:
            try:
                num = int(p.port)
            except Exception:
                continue
            svc = p.service or ''
            status = p.status
            proto = (p.protocol or 'tcp').lower()
            key = f"{num}/{proto}"
            if key not in port_map:
                port_map[key] = {'id': p.id, 'port': num, 'protocol': proto, 'service': svc, 'status': status}
                ordered_ports.append(key)
            else:
                if not port_map[key]['service'] and svc:
                    port_map[key]['service'] = svc
                if port_map[key]['status'] != 'open' and status == 'open':
                    port_map[key]['status'] = 'open'

        return jsonify({
            'success': True,
            'ports': [port_map[key] for key in ordered_ports]
        })
    
    return jsonify({'error': 'Загрузка портов для домена отключена. Используйте загрузку на уровне IP в графе.'}), 400

@app.route('/api/attack-surfaces/<int:attack_surface_id>/domains/<int:domain_id>/technologies', methods=['GET', 'POST'])
def domain_technologies_api(attack_surface_id, domain_id):
    """Получить или сохранить технологии для домена"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    domain = Domain.query.filter_by(id=domain_id, attack_surface_id=attack_surface_id).first_or_404()
    
    if request.method == 'GET':
        # Получить технологии домена
        technologies = AttackSurfaceTechnology.query.filter_by(
            attack_surface_id=attack_surface_id,
            domain_id=domain_id
        ).all()
        
        return jsonify({
            'success': True,
            'technologies': [{
                'id': t.id,
                'name': t.name,
                'version': t.version,
                'category': t.category
            } for t in technologies]
        })
    
    try:
        data = request.get_json()
        if not data or 'technologies' not in data:
            return jsonify({'error': 'Не указаны технологии'}), 400
        
        technologies_data = data['technologies']
        
        # Удаляем старые технологии для этого домена
        AttackSurfaceTechnology.query.filter_by(
            attack_surface_id=attack_surface_id,
            domain_id=domain_id
        ).delete()
        
        # Сохраняем новые технологии
        for tech_info in technologies_data:
            technology = AttackSurfaceTechnology(
                name=tech_info.get('name'),
                version=tech_info.get('version', ''),
                category=tech_info.get('category', ''),
                attack_surface_id=attack_surface_id,
                domain_id=domain_id
            )
            db.session.add(technology)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Технологии успешно сохранены'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка сохранения технологий: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/upload-nmap-ports', methods=['POST'])
def upload_attack_surface_nmap_ports(attack_surface_id):
    """Загрузка портов из nmap XML файла для Attack Surface с сохранением на уровне IP"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    
    try:
        data = request.get_json()
        target_ip = data.get('target_ip')
        ports_data = data.get('ports', [])
        
        if not target_ip:
            return jsonify({'error': 'Не указан целевой IP-адрес'}), 400
            
        if not ports_data:
            return jsonify({'error': 'Нет данных о портах'}), 400
        
        # Найти или создать IP запись
        ip_record = IPAddress.query.filter_by(attack_surface_id=attack_surface_id, ip=target_ip).first()
        if not ip_record:
            ip_record = IPAddress(ip=target_ip, attack_surface_id=attack_surface_id)
            db.session.add(ip_record)
            db.session.flush()  # получить id

        # Привязать IP к существующему CIDR, если подходит
        try:
            ip_addr = ipaddress.ip_address(target_ip)
            cidr_blocks = CIDRBlock.query.filter_by(attack_surface_id=attack_surface_id).all()
            for cb in cidr_blocks:
                try:
                    network = ipaddress.ip_network(cb.cidr, strict=False)
                except ValueError:
                    continue
                if ip_addr in network:
                    if ip_record.cidr_block_id != cb.id:
                        ip_record.cidr_block_id = cb.id
                    break
        except ValueError:
            pass

        # Найти домены на этом IP (для сведений в ответе)
        domains_on_ip = Domain.query.filter_by(
            attack_surface_id=attack_surface_id,
            ip_address_id=ip_record.id
        ).all()

        total_added = 0
        total_skipped = 0
        updated_domains = [d.domain for d in domains_on_ip]

        # Добавляем/обновляем порты на уровне IP
        for port_info in ports_data:
            port_number = port_info.get('port') or port_info.get('number')
            if port_number is None:
                continue
            service = port_info.get('service', '')
            status = port_info.get('status', 'open')
            protocol = (port_info.get('protocol') or 'tcp').lower()

            existing_port = AttackSurfacePort.query.filter_by(
                attack_surface_id=attack_surface_id,
                ip_address_id=ip_record.id,
                port=port_number,
                protocol=protocol
            ).first()

            if existing_port:
                # Обновляем сервис, если ранее был пуст
                if not existing_port.service and service:
                    existing_port.service = service
                    existing_port.status = status
                    total_added += 1
                else:
                    total_skipped += 1
            else:
                new_port = AttackSurfacePort(
                    port=port_number,
                    service=service,
                    status=status,
                    protocol=protocol,
                    attack_surface_id=attack_surface_id,
                    ip_address_id=ip_record.id,
                    domain_id=None
                )
                db.session.add(new_port)
                total_added += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Порты успешно загружены для IP {target_ip}',
            'total_added': total_added,
            'total_skipped': total_skipped,
            'updated_domains': updated_domains,
            'domains_count': len(domains_on_ip)
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка при загрузке портов: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/upload-udp-ports', methods=['POST'])
def upload_attack_surface_udp_ports(attack_surface_id):
    """Загрузка UDP-портов из udpx.txt (JSON/JSONL) для Attack Surface с сохранением на уровне IP"""
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)

    try:
        data = request.get_json()
        target_ip = data.get('target_ip')
        ports_data = data.get('ports', [])

        if not target_ip:
            return jsonify({'error': 'Не указан целевой IP-адрес'}), 400

        if not ports_data:
            return jsonify({'error': 'Нет данных о UDP портах'}), 400

        # Найти или создать IP запись
        ip_record = IPAddress.query.filter_by(attack_surface_id=attack_surface_id, ip=target_ip).first()
        if not ip_record:
            ip_record = IPAddress(ip=target_ip, attack_surface_id=attack_surface_id)
            db.session.add(ip_record)
            db.session.flush()  # получить id

        # Привязать IP к существующему CIDR, если подходит
        try:
            ip_addr = ipaddress.ip_address(target_ip)
            cidr_blocks = CIDRBlock.query.filter_by(attack_surface_id=attack_surface_id).all()
            for cb in cidr_blocks:
                try:
                    network = ipaddress.ip_network(cb.cidr, strict=False)
                except ValueError:
                    continue
                if ip_addr in network:
                    if ip_record.cidr_block_id != cb.id:
                        ip_record.cidr_block_id = cb.id
                    break
        except ValueError:
            pass

        # Найти домены на этом IP (для сведений в ответе)
        domains_on_ip = Domain.query.filter_by(
            attack_surface_id=attack_surface_id,
            ip_address_id=ip_record.id
        ).all()

        total_added = 0
        total_skipped = 0
        updated_domains = [d.domain for d in domains_on_ip]

        # Добавляем/обновляем порты на уровне IP (UDP)
        for port_info in ports_data:
            port_number = port_info.get('port') or port_info.get('number')
            if port_number is None:
                continue
            service = port_info.get('service', '')
            status = port_info.get('status', 'open')
            protocol = (port_info.get('protocol') or 'udp').lower()

            existing_port = AttackSurfacePort.query.filter_by(
                attack_surface_id=attack_surface_id,
                ip_address_id=ip_record.id,
                port=port_number,
                protocol=protocol
            ).first()

            if existing_port:
                # Обновляем сервис, если ранее был пуст
                if not existing_port.service and service:
                    existing_port.service = service
                    existing_port.status = status
                    total_added += 1
                else:
                    total_skipped += 1
            else:
                new_port = AttackSurfacePort(
                    port=port_number,
                    service=service,
                    status=status,
                    protocol=protocol,
                    attack_surface_id=attack_surface_id,
                    ip_address_id=ip_record.id,
                    domain_id=None
                )
                db.session.add(new_port)
                total_added += 1

        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'UDP порты успешно загружены для IP {target_ip}',
            'total_added': total_added,
            'total_skipped': total_skipped,
            'updated_domains': updated_domains,
            'domains_count': len(domains_on_ip)
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка при загрузке UDP портов: {str(e)}'}), 500

# Инициализация базы данных
def init_db():
    """Инициализация базы данных и создание таблиц"""
    with app.app_context():
        # Динамическое определение директории базы данных
        if os.path.exists('/app'):
            # Docker окружение
            db_dir = '/app/instance'
        else:
            # Локальное окружение
            db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
        
        # Проверяем и создаем директорию для базы данных
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, mode=0o755, exist_ok=True)
            print(f"Создана директория: {db_dir}")
        
        # Проверяем права на запись
        if not os.access(db_dir, os.W_OK):
            print(f"Предупреждение: нет прав на запись в {db_dir}")
        
        # Создаем все таблицы
        try:
            db.create_all()
            print("База данных инициализирована")
            
            # Проверяем, какие таблицы были созданы
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            print(f"Созданные таблицы: {', '.join(tables)}")
            
            # Проверяем права доступа к файлу базы данных
            db_file = os.path.join(db_dir, 'website_scanner.db')
            if os.path.exists(db_file):
                import stat
                
                # Изменяем владельца файла на текущего пользователя (если в Docker)
                if os.path.exists('/app') and hasattr(os, 'getuid'):
                    current_uid = os.getuid()
                    current_gid = os.getgid()
                    try:
                        os.chown(db_file, current_uid, current_gid)
                        print(f"Изменен владелец файла базы данных на UID={current_uid}, GID={current_gid}")
                    except Exception as e:
                        print(f"Не удалось изменить владельца файла: {e}")
                
                file_stat = os.stat(db_file)
                permissions = oct(file_stat.st_mode)[-3:]
                owner_uid = file_stat.st_uid
                group_gid = file_stat.st_gid
                print(f"Права доступа к базе данных: {permissions}")
                print(f"Владелец файла: UID={owner_uid}, GID={group_gid}")
                print(f"Текущий пользователь: UID={os.getuid() if hasattr(os, 'getuid') else 'N/A'}")
            
        except Exception as e:
            print(f"Ошибка при создании базы данных: {e}")
            raise
        
        # Проверяем, существует ли столбец screenshot (только если таблица уже существует)
        try:
            with db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA table_info(website)"))
                columns = [row[1] for row in result.fetchall()]
                if 'screenshot' not in columns:
                    conn.execute(text('ALTER TABLE website ADD COLUMN screenshot VARCHAR(500)'))
                    conn.commit()
                    print("Добавлен столбец screenshot")
        except Exception as e:
            print(f"Предупреждение при проверке столбца screenshot: {e}")
            pass

        # Миграция: добавить столбец protocol в таблицу attack_surface_port, если отсутствует
        try:
            with db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA table_info(attack_surface_port)"))
                columns = [row[1] for row in result.fetchall()]
                if 'protocol' not in columns:
                    conn.execute(text("ALTER TABLE attack_surface_port ADD COLUMN protocol VARCHAR(10) DEFAULT 'tcp'"))
                    conn.commit()
                    print("Добавлен столбец protocol в attack_surface_port")
                if 'is_web' not in columns:
                    conn.execute(text("ALTER TABLE attack_surface_port ADD COLUMN is_web BOOLEAN DEFAULT 0"))
                    conn.commit()
                    print("Добавлен столбец is_web в attack_surface_port")
                if 'web_scheme' not in columns:
                    conn.execute(text("ALTER TABLE attack_surface_port ADD COLUMN web_scheme VARCHAR(10)"))
                    conn.commit()
                    print("Добавлен столбец web_scheme в attack_surface_port")
                if 'web_status_code' not in columns:
                    conn.execute(text("ALTER TABLE attack_surface_port ADD COLUMN web_status_code INTEGER"))
                    conn.commit()
                    print("Добавлен столбец web_status_code в attack_surface_port")
                if 'web_final_url' not in columns:
                    conn.execute(text("ALTER TABLE attack_surface_port ADD COLUMN web_final_url VARCHAR(500)"))
                    conn.commit()
                    print("Добавлен столбец web_final_url в attack_surface_port")
                if 'web_checked_at' not in columns:
                    conn.execute(text("ALTER TABLE attack_surface_port ADD COLUMN web_checked_at DATETIME"))
                    conn.commit()
                    print("Добавлен столбец web_checked_at в attack_surface_port")
        except Exception as e:
            print(f"Предупреждение при проверке/добавлении столбца protocol: {e}")

        # Миграция: добавить столбцы ASN/Organization в таблицу cidr_block, если отсутствуют
        try:
            with db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA table_info(cidr_block)"))
                columns = [row[1] for row in result.fetchall()]
                if 'asn' not in columns:
                    conn.execute(text("ALTER TABLE cidr_block ADD COLUMN asn VARCHAR(20)"))
                    conn.commit()
                    print("Добавлен столбец asn в cidr_block")
                if 'organization' not in columns:
                    conn.execute(text("ALTER TABLE cidr_block ADD COLUMN organization VARCHAR(255)"))
                    conn.commit()
                    print("Добавлен столбец organization в cidr_block")
                if 'network_name' not in columns:
                    conn.execute(text("ALTER TABLE cidr_block ADD COLUMN network_name VARCHAR(255)"))
                    conn.commit()
                    print("Добавлен столбец network_name в cidr_block")
        except Exception as e:
            print(f"Предупреждение при проверке/добавлении столбцов в cidr_block: {e}")

        # Миграция: добавить поля скриншотов в таблицы domain и ip_address, если отсутствуют
        try:
            with db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA table_info(domain)"))
                columns = [row[1] for row in result.fetchall()]
                if 'screenshot' not in columns:
                    conn.execute(text("ALTER TABLE domain ADD COLUMN screenshot VARCHAR(500)"))
                    conn.commit()
                if 'screenshot_status_code' not in columns:
                    conn.execute(text("ALTER TABLE domain ADD COLUMN screenshot_status_code INTEGER"))
                    conn.commit()
                if 'screenshot_url' not in columns:
                    conn.execute(text("ALTER TABLE domain ADD COLUMN screenshot_url VARCHAR(500)"))
                    conn.commit()
                if 'screenshot_checked_at' not in columns:
                    conn.execute(text("ALTER TABLE domain ADD COLUMN screenshot_checked_at DATETIME"))
                    conn.commit()
        except Exception as e:
            print(f"Предупреждение при добавлении столбцов в domain: {e}")
        try:
            with db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA table_info(ip_address)"))
                columns = [row[1] for row in result.fetchall()]
                if 'screenshot' not in columns:
                    conn.execute(text("ALTER TABLE ip_address ADD COLUMN screenshot VARCHAR(500)"))
                    conn.commit()
                if 'screenshot_status_code' not in columns:
                    conn.execute(text("ALTER TABLE ip_address ADD COLUMN screenshot_status_code INTEGER"))
                    conn.commit()
                if 'screenshot_url' not in columns:
                    conn.execute(text("ALTER TABLE ip_address ADD COLUMN screenshot_url VARCHAR(500)"))
                    conn.commit()
                if 'screenshot_checked_at' not in columns:
                    conn.execute(text("ALTER TABLE ip_address ADD COLUMN screenshot_checked_at DATETIME"))
                    conn.commit()
                result = conn.execute(text("PRAGMA table_info(attack_surface_scope)"))
                cols_scope = [row[1] for row in result.fetchall()]
                if len(cols_scope) == 0:
                    conn.execute(text("CREATE TABLE IF NOT EXISTS attack_surface_scope (id INTEGER PRIMARY KEY AUTOINCREMENT, attack_surface_id INTEGER NOT NULL, item VARCHAR(255) NOT NULL, created_at DATETIME, FOREIGN KEY(attack_surface_id) REFERENCES attack_surface(id))"))
                    conn.commit()
        except Exception as e:
            print(f"Предупреждение при добавлении столбцов в ip_address: {e}")

# Модели базы данных
class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(50), default='Активен')  # 'Активен', 'Завершен', 'Приостановлен'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    websites = db.relationship('Website', backref='project', lazy=True, cascade='all, delete-orphan')
    attack_surfaces = db.relationship('AttackSurface', backref='project', lazy=True, cascade='all, delete-orphan')

class Website(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), default='Не начат')  # 'Не начат' или 'В работе'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    screenshot = db.Column(db.String(500))  # Путь к скриншоту
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=True)  # Связь с проектом
    
    # Базовая информация (оставляем для обратной совместимости)
    technologies = db.Column(db.Text)
    ports = db.Column(db.Text)
    certificates = db.Column(db.Text)
    files = db.Column(db.Text)
    routes = db.Column(db.Text)
    directories = db.Column(db.Text)
    
    # Связи
    functions = db.relationship('SiteFunction', backref='website', lazy=True, cascade='all, delete-orphan')
    website_technologies = db.relationship('Technology', backref='website', lazy=True, cascade='all, delete-orphan')
    website_ports = db.relationship('Port', backref='website', lazy=True, cascade='all, delete-orphan')

class Technology(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    version = db.Column(db.String(50))
    website_id = db.Column(db.Integer, db.ForeignKey('website.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Port(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False)
    service = db.Column(db.String(100))
    status = db.Column(db.String(20), default='open')
    website_id = db.Column(db.Integer, db.ForeignKey('website.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SiteFunction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    website_id = db.Column(db.Integer, db.ForeignKey('website.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('site_function.id'), nullable=True)
    status = db.Column(db.String(50), default='active')  # active, inactive, testing, completed
    
    # Связи
    children = db.relationship('SiteFunction', backref=db.backref('parent', remote_side=[id]), lazy=True)
    endpoints = db.relationship('Endpoint', backref='function', lazy=True, cascade='all, delete-orphan')
    notes = db.relationship('Note', backref='function', lazy=True, cascade='all, delete-orphan')

class Endpoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    method = db.Column(db.String(10), default='GET')
    parameters = db.Column(db.Text)
    headers = db.Column(db.Text)
    response_info = db.Column(db.Text)
    function_id = db.Column(db.Integer, db.ForeignKey('site_function.id'), nullable=False)
    status = db.Column(db.String(50), default='Не начат')

class Note(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    function_id = db.Column(db.Integer, db.ForeignKey('site_function.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# Модели для Attack Surface
class AttackSurface(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    cidr_blocks = db.relationship('CIDRBlock', backref='attack_surface', lazy=True, cascade='all, delete-orphan')
    ip_addresses = db.relationship('IPAddress', backref='attack_surface', lazy=True, cascade='all, delete-orphan')
    domains = db.relationship('Domain', backref='attack_surface', lazy=True, cascade='all, delete-orphan')
    # scope items relation will be available after table creation

class CIDRBlock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cidr = db.Column(db.String(50), nullable=False)  # например, 192.168.1.0/24
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    asn = db.Column(db.String(20))
    organization = db.Column(db.String(255))
    network_name = db.Column(db.String(255))
    
    # Связи
    ip_addresses = db.relationship('IPAddress', backref='cidr_block', lazy=True)

class IPAddress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(45), nullable=False)  # поддержка IPv4 и IPv6
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    cidr_block_id = db.Column(db.Integer, db.ForeignKey('cidr_block.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    domains = db.relationship('Domain', backref='ip_address', lazy=True)
    ports = db.relationship('AttackSurfacePort', backref='ip_address', lazy=True, cascade='all, delete-orphan')
    screenshot = db.Column(db.String(500))
    screenshot_status_code = db.Column(db.Integer)
    screenshot_url = db.Column(db.String(500))
    screenshot_checked_at = db.Column(db.DateTime)

class Domain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(255), nullable=False)
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    ip_address_id = db.Column(db.Integer, db.ForeignKey('ip_address.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    ports = db.relationship('AttackSurfacePort', backref='domain', lazy=True, cascade='all, delete-orphan')
    technologies = db.relationship('AttackSurfaceTechnology', backref='domain', lazy=True, cascade='all, delete-orphan')
    screenshot = db.Column(db.String(500))
    screenshot_status_code = db.Column(db.Integer)
    screenshot_url = db.Column(db.String(500))
    screenshot_checked_at = db.Column(db.DateTime)

class AttackSurfaceScope(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    item = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AttackSurfacePort(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    port = db.Column(db.Integer, nullable=False)
    service = db.Column(db.String(100))
    status = db.Column(db.String(20), default='open')
    protocol = db.Column(db.String(10), default='tcp')
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    ip_address_id = db.Column(db.Integer, db.ForeignKey('ip_address.id'), nullable=True)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_web = db.Column(db.Boolean, default=False)
    web_scheme = db.Column(db.String(10))
    web_status_code = db.Column(db.Integer)
    web_final_url = db.Column(db.String(500))
    web_checked_at = db.Column(db.DateTime)

class AttackSurfaceTechnology(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    version = db.Column(db.String(50))
    category = db.Column(db.String(100))
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    domain_id = db.Column(db.Integer, db.ForeignKey('domain.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AttackSurfaceVhostFinding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attack_surface_id = db.Column(db.Integer, db.ForeignKey('attack_surface.id'), nullable=False)
    target = db.Column(db.String(255))
    entry = db.Column(db.String(255), nullable=False)
    suffix = db.Column(db.String(255))
    full_domain = db.Column(db.String(255))
    status = db.Column(db.String(100))
    exists_in_attack_surface = db.Column(db.Boolean, default=False)
    resolve_ok = db.Column(db.Boolean, default=False)
    resolved_ip = db.Column(db.String(64))
    source_filename = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Инициализация базы данных будет выполнена при запуске приложения

# Маршруты
@app.route('/')
def index():
    projects = Project.query.all()
    # Добавляем количество сайтов для каждого проекта
    for project in projects:
        project.websites_count = Website.query.filter_by(project_id=project.id).count()
    return render_template('projects.html', projects=projects)

@app.route('/project/<int:project_id>')
def project_detail(project_id):
    project = Project.query.get_or_404(project_id)
    websites = Website.query.filter_by(project_id=project_id).all()
    return render_template('project_detail.html', project=project, websites=websites)

@app.route('/websites')
def all_websites():
    websites = Website.query.all()
    projects = Project.query.all()
    return render_template('index.html', websites=websites, projects=projects)

@app.route('/website/<int:website_id>')
def website_detail(website_id):
    website = Website.query.get_or_404(website_id)
    ports = Port.query.filter_by(website_id=website_id).all()
    technologies = Technology.query.filter_by(website_id=website_id).all()
    
    # Получаем информацию о проекте и других сайтах для навигации
    project = None
    project_websites = []
    if website.project_id:
        project = Project.query.get(website.project_id)
        project_websites = Website.query.filter_by(project_id=website.project_id).all()
    
    return render_template('website_detail.html', 
                         website=website, 
                         ports=ports, 
                         technologies=technologies,
                         project=project,
                         project_websites=project_websites)

@app.route('/website/<int:website_id>/csv-analyzer')
def csv_analyzer(website_id):
    website = Website.query.get_or_404(website_id)
    return render_template('csv_analyzer.html', website=website)

@app.route('/attack-surface')
def attack_surface():
    return render_template('attack_surface.html')

@app.route('/api/attack-surfaces/<int:attack_surface_id>/vhost/findings', methods=['GET'])
def vhost_findings(attack_surface_id):
    AttackSurface.query.get_or_404(attack_surface_id)
    q = AttackSurfaceVhostFinding.query.filter_by(attack_surface_id=attack_surface_id).order_by(AttackSurfaceVhostFinding.id.desc()).all()
    return jsonify({
        'success': True,
        'count': len(q),
        'findings': [
            {
                'id': f.id,
                'target': f.target,
                'entry': f.entry,
                'suffix': f.suffix,
                'full_domain': f.full_domain,
                'status': f.status,
                'exists_in_attack_surface': bool(f.exists_in_attack_surface),
                'resolve_ok': bool(f.resolve_ok),
                'resolved_ip': f.resolved_ip,
                'source_filename': f.source_filename,
                'created_at': f.created_at.isoformat() if f.created_at else None
            } for f in q
        ]
    })

@app.route('/api/attack-surfaces/<int:attack_surface_id>/vhost/resolve', methods=['POST'])
def vhost_resolve(attack_surface_id):
    AttackSurface.query.get_or_404(attack_surface_id)
    data = request.get_json(silent=True) or {}
    domains = data.get('domains') or []
    result = {}
    for d in domains:
        host = str(d).strip()
        ip = resolve_domain_to_ip(host)
        if ip:
            result[host] = {'resolve_ok': True, 'ip': ip}
        else:
            result[host] = {'resolve_ok': False, 'ip': None}
    return jsonify({'success': True, 'results': result})

@app.route('/api/attack-surfaces/<int:attack_surface_id>/vhost/import', methods=['POST'])
def vhost_import(attack_surface_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    try:
        data = request.get_json(silent=True) or {}
        files = data.get('files') or []
        suffix = (data.get('suffix') or '').strip()
        doms = Domain.query.filter_by(attack_surface_id=attack_surface_id).all()
        existing_hosts = set()
        for d in doms:
            val = str(d.domain or '').strip().lower()
            try:
                if val.startswith(('http://','https://')):
                    u = urlparse(val)
                    val = u.netloc.split(':',1)[0].lower()
            except Exception:
                pass
            if val.startswith('www.'):
                existing_hosts.add(val[4:])
            existing_hosts.add(val)
        saved = []
        seen_in_batch = set()
        for f in files:
            filename = str(f.get('filename') or '').strip()
            target = str(f.get('target') or '').strip()
            entries = f.get('entries') or []
            for e in entries:
                entry = str(e.get('entry') or '').strip()
                status = str(e.get('status') or '').strip()
                full = str(e.get('full_domain') or '').strip()
                if not full:
                    if suffix:
                        full = f"{entry}.{suffix}".strip()
                    else:
                        full = entry
                host = full.lower()
                if host.startswith(('http://','https://')):
                    try:
                        u = urlparse(host)
                        host = u.netloc.split(':',1)[0].lower()
                    except Exception:
                        host = host.replace('http://','').replace('https://','').split('/')[0].lower()
                if host.startswith('www.'):
                    host_n = host[4:]
                else:
                    host_n = host
                if host in seen_in_batch:
                    continue
                seen_in_batch.add(host)
                exists = (host in existing_hosts) or (host_n in existing_hosts)
                ip = resolve_domain_to_ip(host)
                resolve_ok = bool(ip)
                existing_finding = AttackSurfaceVhostFinding.query.filter_by(attack_surface_id=attack_surface.id, full_domain=host).first()
                if existing_finding:
                    existing_finding.target = target if target else existing_finding.target
                    existing_finding.entry = entry or existing_finding.entry
                    existing_finding.suffix = suffix if suffix else existing_finding.suffix
                    existing_finding.status = status if status else existing_finding.status
                    existing_finding.exists_in_attack_surface = exists
                    existing_finding.resolve_ok = resolve_ok
                    existing_finding.resolved_ip = ip if ip else existing_finding.resolved_ip
                    existing_finding.source_filename = filename if filename else existing_finding.source_filename
                    saved.append({'target': existing_finding.target, 'entry': existing_finding.entry, 'full_domain': existing_finding.full_domain, 'status': existing_finding.status, 'exists_in_attack_surface': existing_finding.exists_in_attack_surface, 'resolve_ok': existing_finding.resolve_ok, 'resolved_ip': existing_finding.resolved_ip, 'source_filename': existing_finding.source_filename})
                else:
                    rec = AttackSurfaceVhostFinding(
                        attack_surface_id=attack_surface.id,
                        target=target if target else None,
                        entry=entry,
                        suffix=suffix if suffix else None,
                        full_domain=host,
                        status=status if status else None,
                        exists_in_attack_surface=exists,
                        resolve_ok=resolve_ok,
                        resolved_ip=ip if ip else None,
                        source_filename=filename if filename else None
                    )
                    db.session.add(rec)
                    saved.append({'target': rec.target, 'entry': rec.entry, 'full_domain': rec.full_domain, 'status': rec.status, 'exists_in_attack_surface': rec.exists_in_attack_surface, 'resolve_ok': rec.resolve_ok, 'resolved_ip': rec.resolved_ip, 'source_filename': rec.source_filename})
        db.session.commit()
        return jsonify({'success': True, 'saved': saved})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка импорта VHost: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/vhost/clear', methods=['DELETE','POST'])
def vhost_clear(attack_surface_id):
    try:
        AttackSurface.query.get(attack_surface_id)
        deleted = AttackSurfaceVhostFinding.query.filter_by(attack_surface_id=attack_surface_id).delete()
        db.session.commit()
        return jsonify({'success': True, 'deleted': int(deleted) if deleted is not None else 0})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка очистки VHost: {str(e)}'}), 500

@app.route('/api/attack-surfaces/<int:attack_surface_id>/scope', methods=['GET', 'POST'])
def manage_scope(attack_surface_id):
    attack_surface = AttackSurface.query.get_or_404(attack_surface_id)
    if request.method == 'GET':
        try:
            items = [s.item for s in AttackSurfaceScope.query.filter_by(attack_surface_id=attack_surface_id).all()]
            return jsonify({'success': True, 'items': items, 'scope_only': False})
        except Exception as e:
            return jsonify({'error': f'Ошибка получения scope: {str(e)}'}), 500
    else:
        try:
            data = request.get_json(silent=True) or {}
            items = [str(x).strip() for x in (data.get('items') or []) if str(x).strip()]
            AttackSurfaceScope.query.filter_by(attack_surface_id=attack_surface_id).delete()
            for it in items:
                s = AttackSurfaceScope(attack_surface_id=attack_surface_id, item=it)
                db.session.add(s)
            db.session.commit()
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка сохранения scope: {str(e)}'}), 500

@app.route('/test-save')
def test_save():
    """Тестовая страница для проверки сохранения"""
    with open('test_save.html', 'r', encoding='utf-8') as f:
        return f.read()

@app.route('/static/uploads/screenshots/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# API маршруты для работы с проектами
@app.route('/api/projects', methods=['GET'])
def get_projects():
    projects = Project.query.all()
    return jsonify([{
        'id': p.id,
        'name': p.name,
        'description': p.description,
        'status': p.status,
        'created_at': p.created_at.isoformat() if p.created_at else None,
        'updated_at': p.updated_at.isoformat() if p.updated_at else None
    } for p in projects])

@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.get_json()
    
    project = Project(
        name=data['name'],
        description=data.get('description', ''),
        status=data.get('status', 'Активный')
    )
    
    db.session.add(project)
    db.session.commit()
    
    return jsonify({
        'id': project.id,
        'name': project.name,
        'description': project.description,
        'status': project.status,
        'created_at': project.created_at.isoformat() if project.created_at else None,
        'updated_at': project.updated_at.isoformat() if project.updated_at else None
    })

@app.route('/api/projects/<int:project_id>', methods=['GET', 'PUT', 'DELETE'])
def api_project_detail(project_id):
    project = Project.query.get_or_404(project_id)
    
    if request.method == 'GET':
        return jsonify({
            'id': project.id,
            'name': project.name,
            'description': project.description,
            'status': project.status,
            'created_at': project.created_at.isoformat() if project.created_at else None,
            'updated_at': project.updated_at.isoformat() if project.updated_at else None
        })
    
    elif request.method == 'PUT':
        data = request.get_json()
        project.name = data.get('name', project.name)
        project.description = data.get('description', project.description)
        project.status = data.get('status', project.status)
        
        db.session.commit()
        
        return jsonify({
            'id': project.id,
            'name': project.name,
            'description': project.description,
            'status': project.status,
            'created_at': project.created_at.isoformat() if project.created_at else None,
            'updated_at': project.updated_at.isoformat() if project.updated_at else None
        })
    
    elif request.method == 'DELETE':
        # Получаем количество сайтов для информирования пользователя
        websites_count = Website.query.filter_by(project_id=project_id).count()
        
        # Удаляем проект (каскадное удаление автоматически удалит все связанные сайты)
        db.session.delete(project)
        db.session.commit()
        
        if websites_count > 0:
            return jsonify({'message': f'Проект и {websites_count} связанных сайтов удалены успешно'})
        else:
            return jsonify({'message': 'Проект удален успешно'})

@app.route('/api/websites', methods=['GET', 'POST'])
def api_websites():
    if request.method == 'POST':
        try:
            ct = (request.content_type or '').lower()
            is_multipart = ct.startswith('multipart/form-data')

            if is_multipart:
                project_id = request.form.get('project_id')
                pid = int(project_id) if project_id else None
                file = request.files.get('screenshot')
                screenshot_path = None
                if file and file.filename != '' and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
                    filename = timestamp + filename
                    out_dir, rel_dir = _get_project_screenshots_dir(pid)
                    try:
                        os.makedirs(out_dir, exist_ok=True)
                    except Exception:
                        pass
                    file_path = os.path.join(out_dir, filename)
                    file.save(file_path)
                    screenshot_path = f'{rel_dir}/{filename}'
                name = request.form.get('name')
                url = request.form.get('url')
                status = request.form.get('status', 'Не начат')
                if not screenshot_path and url:
                    try:
                        shot = capture_screenshot_headless(url, delay_ms=3000, project_id=pid)
                        if shot:
                            screenshot_path = shot
                    except Exception:
                        screenshot_path = None
            else:
                data = request.get_json(silent=True) or {}
                name = data.get('name')
                url = data.get('url')
                status = data.get('status', 'Не начат')
                screenshot_path = None
                project_id = data.get('project_id')
                pid = int(project_id) if project_id else None
                try:
                    if url:
                        shot = capture_screenshot_headless(url, delay_ms=3000, project_id=pid)
                        if shot:
                            screenshot_path = shot
                except Exception:
                    screenshot_path = None

            if not name or not url:
                return jsonify({'success': False, 'message': 'Имя и URL обязательны для заполнения'}), 400

            website = Website(
                name=name,
                url=url,
                status=status,
                screenshot=screenshot_path,
                project_id=int(project_id) if project_id else None
            )
            db.session.add(website)
            db.session.commit()
            return jsonify({
                'success': True,
                'message': 'Сайт создан успешно',
                'website': {
                    'id': website.id,
                    'name': website.name,
                    'url': website.url,
                    'status': website.status
                }
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': f'Ошибка при создании сайта: {str(e)}'}), 500
    
    websites = Website.query.all()
    return jsonify([{
        'id': w.id,
        'name': w.name,
        'url': w.url,
        'status': w.status,
        'screenshot': w.screenshot,
        'created_at': w.created_at.isoformat()
    } for w in websites])

@app.route('/api/websites/<int:website_id>', methods=['GET', 'PUT', 'DELETE'])
def api_website_detail(website_id):
    website = Website.query.get_or_404(website_id)
    
    if request.method == 'GET':
        return jsonify({
            'id': website.id,
            'name': website.name,
            'url': website.url,
            'status': website.status,
            'screenshot': website.screenshot,
            'project_id': website.project_id,
            'technologies': website.technologies,
            'ports': website.ports,
            'certificates': website.certificates,
            'files': website.files,
            'routes': website.routes,
            'directories': website.directories,
            'created_at': website.created_at.isoformat()
        })
    
    elif request.method == 'PUT':
        ct = (request.content_type or '').lower()
        is_multipart = ct.startswith('multipart/form-data')
        if is_multipart:
            file = request.files.get('screenshot')
            screenshot_path = website.screenshot
            project_id = request.form.get('project_id')
            pid = int(project_id) if project_id else website.project_id
            if file and file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
                filename = timestamp + filename
                out_dir, rel_dir = _get_project_screenshots_dir(pid)
                try:
                    os.makedirs(out_dir, exist_ok=True)
                except Exception:
                    pass
                file_path = os.path.join(out_dir, filename)
                file.save(file_path)
                screenshot_path = f'{rel_dir}/{filename}'
            website.name = request.form.get('name', website.name)
            website.url = request.form.get('url', website.url)
            website.status = request.form.get('status', website.status)
            website.screenshot = screenshot_path
            website.project_id = int(project_id) if project_id else None
            website.technologies = request.form.get('technologies', website.technologies)
            website.ports = request.form.get('ports', website.ports)
            website.certificates = request.form.get('certificates', website.certificates)
            website.files = request.form.get('files', website.files)
            website.routes = request.form.get('routes', website.routes)
            website.directories = request.form.get('directories', website.directories)
        else:
            data = request.get_json(silent=True) or {}
            website.name = data.get('name', website.name)
            website.url = data.get('url', website.url)
            website.status = data.get('status', website.status)
            if 'project_id' in data:
                website.project_id = data['project_id']
            website.technologies = data.get('technologies', website.technologies)
            website.ports = data.get('ports', website.ports)
            website.certificates = data.get('certificates', website.certificates)
            website.files = data.get('files', website.files)
            website.routes = data.get('routes', website.routes)
            website.directories = data.get('directories', website.directories)
        
        db.session.commit()
        
        # Автоматическая миграция технологий и портов после обновления
        try:
            migrated_count = 0
            
            # Миграция технологий
            if website.technologies:
                tech_list = [tech.strip() for tech in website.technologies.split(',') if tech.strip()]
                for tech_entry in tech_list:
                    # Разделяем строку на отдельные технологии (например, "Laravel Apache 2.40.2 Nginx")
                    individual_techs = parse_multiple_technologies(tech_entry)
                    
                    for tech_name, tech_version in individual_techs:
                        # Проверяем, не существует ли уже такая технология
                        existing = Technology.query.filter_by(website_id=website_id, name=tech_name, version=tech_version).first()
                        if not existing:
                            technology = Technology(name=tech_name, version=tech_version, website_id=website_id)
                            db.session.add(technology)
                            migrated_count += 1
            
            # Миграция портов
            if website.ports:
                # Поддерживаем как разделение запятыми, так и переносами строк
                if ',' in website.ports:
                    port_list = [port.strip() for port in website.ports.split(',') if port.strip()]
                else:
                    port_list = [port.strip() for port in website.ports.split('\n') if port.strip()]
                for port_str in port_list:
                    try:
                        # Извлекаем номер порта (может быть в формате "80", "80/tcp", "80 (http)")
                        port_number = int(port_str.split('/')[0].split('(')[0].strip())
                        
                        # Извлекаем сервис, если указан
                        service = ''
                        if '(' in port_str and ')' in port_str:
                            service = port_str.split('(')[1].split(')')[0].strip()
                        
                        # Проверяем, не существует ли уже такой порт
                        existing = Port.query.filter_by(website_id=website_id, number=port_number).first()
                        if not existing:
                            port = Port(number=port_number, service=service, website_id=website_id)
                            db.session.add(port)
                            migrated_count += 1
                    except ValueError:
                        # Пропускаем некорректные номера портов
                        continue
            
            if migrated_count > 0:
                db.session.commit()
                
        except Exception as e:
            # Если миграция не удалась, не прерываем основной процесс
            print(f"Ошибка автоматической миграции: {str(e)}")
        
        return jsonify({'message': 'Сайт обновлен успешно'})
    
    elif request.method == 'DELETE':
        db.session.delete(website)
        db.session.commit()
        return jsonify({'message': 'Сайт удален успешно'})

@app.route('/api/websites/<int:website_id>/functions', methods=['GET', 'POST'])
def api_functions(website_id):
    website = Website.query.get_or_404(website_id)
    
    if request.method == 'POST':
        data = request.get_json()
        function = SiteFunction(
            name=data['name'],
            description=data.get('description', ''),
            website_id=website_id,
            parent_id=data.get('parent_id'),
            status=data.get('status', 'active')
        )
        db.session.add(function)
        db.session.commit()
        return jsonify({'id': function.id, 'message': 'Функция создана успешно'})
    
    functions = SiteFunction.query.filter_by(website_id=website_id).all()
    return jsonify([{
        'id': f.id,
        'name': f.name,
        'description': f.description,
        'parent_id': f.parent_id,
        'status': f.status,
        'children_count': len(f.children),
        'endpoints_count': len(f.endpoints),
        'notes_count': len(f.notes)
    } for f in functions])

@app.route('/api/functions/<int:function_id>/endpoints', methods=['GET', 'POST'])
def api_endpoints(function_id):
    function = SiteFunction.query.get_or_404(function_id)
    
    if request.method == 'POST':
        data = request.get_json()
        endpoint = Endpoint(
            url=data['url'],
            method=data.get('method', 'GET'),
            parameters=data.get('parameters', ''),
            headers=data.get('headers', ''),
            response_info=data.get('response_info', ''),
            function_id=function_id,
            status=data.get('status', 'Не начат')
        )
        db.session.add(endpoint)
        db.session.commit()
        return jsonify({'id': endpoint.id, 'message': 'Endpoint создан успешно'})
    
    endpoints = Endpoint.query.filter_by(function_id=function_id).all()
    return jsonify([{
        'id': e.id,
        'url': e.url,
        'method': e.method,
        'parameters': e.parameters,
        'headers': e.headers,
        'response_info': e.response_info,
        'status': e.status
    } for e in endpoints])

@app.route('/api/functions/<int:function_id>/notes', methods=['GET', 'POST'])
def api_notes(function_id):
    function = SiteFunction.query.get_or_404(function_id)
    
    if request.method == 'POST':
        data = request.get_json()
        note = Note(
            title=data['title'],
            content=data['content'],
            function_id=function_id
        )
        db.session.add(note)
        db.session.commit()
        return jsonify({'id': note.id, 'message': 'Заметка создана успешно'})
    
    notes = Note.query.filter_by(function_id=function_id).all()
    return jsonify([{
        'id': n.id,
        'title': n.title,
        'content': n.content,
        'created_at': n.created_at.isoformat(),
        'updated_at': n.updated_at.isoformat() if n.updated_at else None
    } for n in notes])

@app.route('/api/endpoints/<int:endpoint_id>', methods=['GET', 'PUT', 'DELETE'])
def api_endpoint_detail(endpoint_id):
    endpoint = Endpoint.query.get_or_404(endpoint_id)
    
    if request.method == 'GET':
        return jsonify({
            'id': endpoint.id,
            'function_id': endpoint.function_id,
            'url': endpoint.url,
            'method': endpoint.method,
            'parameters': endpoint.parameters,
            'headers': endpoint.headers,
            'response_info': endpoint.response_info,
            'status': endpoint.status
        })
    
    elif request.method == 'PUT':
        data = request.get_json()
        endpoint.url = data.get('url', endpoint.url)
        endpoint.method = data.get('method', endpoint.method)
        endpoint.parameters = data.get('parameters', endpoint.parameters)
        endpoint.headers = data.get('headers', endpoint.headers)
        endpoint.response_info = data.get('response_info', endpoint.response_info)
        endpoint.status = data.get('status', endpoint.status)
        db.session.commit()
        return jsonify({'message': 'Endpoint обновлен успешно'})
    
    elif request.method == 'DELETE':
        db.session.delete(endpoint)
        db.session.commit()
        return jsonify({'message': 'Endpoint удален успешно'})

@app.route('/api/functions/<int:function_id>/raw-endpoint', methods=['POST'])
def api_raw_endpoint(function_id):
    """Парсит raw HTTP запрос и создает endpoint"""
    function = SiteFunction.query.get_or_404(function_id)
    
    data = request.get_json()
    raw_request = data.get('raw_request', '')
    raw_response = data.get('raw_response', '')
    status = data.get('status', 'Не начат')
    
    if not raw_request:
        return jsonify({'error': 'Raw запрос не предоставлен'}), 400
    
    try:
        # Парсим raw запрос
        parsed_data = parse_raw_request(raw_request)
        
        # Создаем endpoint
        endpoint = Endpoint(
            url=parsed_data['url'],
            method=parsed_data['method'],
            parameters=parsed_data['parameters'],
            headers=parsed_data['headers'],
            response_info=raw_response if raw_response else '',
            function_id=function_id,
            status=status
        )
        
        db.session.add(endpoint)
        db.session.commit()
        
        return jsonify({
            'id': endpoint.id,
            'message': 'Endpoint создан из raw запроса успешно',
            'parsed_data': parsed_data
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка при парсинге raw запроса: {str(e)}'}), 400

@app.route('/api/notes/<int:note_id>', methods=['GET', 'PUT', 'DELETE'])
def api_note_detail(note_id):
    note = Note.query.get_or_404(note_id)
    
    if request.method == 'GET':
        return jsonify({
            'id': note.id,
            'function_id': note.function_id,
            'title': note.title,
            'content': note.content,
            'created_at': note.created_at.isoformat(),
            'updated_at': note.updated_at.isoformat()
        })
    
    elif request.method == 'PUT':
        data = request.get_json()
        note.title = data.get('title', note.title)
        note.content = data.get('content', note.content)
        note.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'message': 'Заметка обновлена успешно'})
    
    elif request.method == 'DELETE':
        db.session.delete(note)
        db.session.commit()
        return jsonify({'message': 'Заметка удалена успешно'})

@app.route('/api/websites/<int:website_id>/analyze-fuzz', methods=['POST'])
def analyze_fuzz_data(website_id):
    """Анализирует CSV данные фаззинга и возвращает статистику для фильтрации"""
    website = Website.query.get_or_404(website_id)
    
    try:
        data = request.get_json()
        
        if 'csv_data' not in data:
            return jsonify({'error': 'CSV данные не предоставлены'}), 400
        
        csv_text = data['csv_data']
        stats = analyze_fuzz_csv_text(csv_text)
        
        if stats is None:
            return jsonify({'error': 'Ошибка при анализе CSV данных'}), 500
        
        # Преобразуем defaultdict в обычные словари для JSON сериализации
        response_stats = {
            'total_records': stats['total_records'],
            'status_codes': dict(stats['status_codes']),
            'content_lengths': dict(sorted(stats['content_lengths'].items())[:20]),  # Топ 20 длин
            'content_lines': dict(sorted(stats['content_lines'].items())[:20]),      # Топ 20 количеств строк
            'content_words': dict(sorted(stats['content_words'].items())[:20]),      # Топ 20 количеств слов
            'records_by_status': {str(k): v for k, v in stats['records_by_status'].items()},
            'unique_response_types': stats['unique_response_types'],
            'response_groups': {
                k: {
                    'count': v['count'],
                    'fingerprint': v['fingerprint'],
                    'status_code': v['status_code'],
                    'examples': v['examples'],
                    'records': v['records']  # Добавляем все записи, а не только примеры
                } for k, v in sorted(stats['response_groups'].items(), key=lambda x: x[1]['count'], reverse=True)[:50]  # Топ 50 групп по количеству
            }
        }
        
        return jsonify({
            'message': 'Анализ CSV данных завершен',
            'stats': response_stats
        })
        
    except Exception as e:
        print(f"Детальная ошибка при анализе: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Ошибка при анализе: {str(e)}'}), 500

@app.route('/api/websites/<int:website_id>/import-fuzz', methods=['POST'])
def import_fuzz_data(website_id):
    """Импортирует данные из CSV файла фаззинга с учетом фильтров исключения или отфильтрованные данные"""
    website = Website.query.get_or_404(website_id)
    
    try:
        data = request.get_json()
        
        # Проверяем, есть ли отфильтрованные данные из анализатора
        if 'filtered_stats' in data:
            # Обрабатываем отфильтрованные данные из CSV-анализатора
            filtered_stats = data['filtered_stats']
            print(f"Получены отфильтрованные данные: {filtered_stats}")
            
            files = []
            directories = []
            routes = []
            
            # Извлекаем данные из отфильтрованной статистики
            # Проверяем, есть ли данные в response_groups (новый формат)
            if 'response_groups' in filtered_stats:
                print(f"Обрабатываем response_groups, групп: {len(filtered_stats['response_groups'])}")
                for group_id, group_data in filtered_stats['response_groups'].items():
                    # Используем все записи из группы, а не только примеры
                    records = group_data.get('records', [])
                    if not records:  # Fallback на examples только если records пустой
                        records = group_data.get('examples', [])
                    print(f"Группа {group_id}: записей {len(records)}")
                    for record in records:
                        # Правильно извлекаем FUZZ значение
                        fuzz_value = record.get('FUZZ', record.get('fuzz', record.get('fuzz_value', record.get('url', ''))))
                        url = record.get('url', '')
                        content_length = record.get('content_length', 0)
                        
                        # Извлекаем полный путь из URL
                        if url:
                            full_path = extract_path_from_url(url)
                            display_name = full_path if full_path else fuzz_value
                        else:
                            display_name = fuzz_value
                        
                        print(f"Обрабатываем запись: {display_name}, длина: {content_length}")
                        
                        # Классифицируем запись
                        redirect = record.get('redirectlocation', record.get('redirect', ''))
                        status_code = record.get('status_code', group_data.get('status_code', 0))
                        
                        if classify_as_file(display_name):
                            files.append({
                                'name': display_name,
                                'status_code': int(status_code),
                                'content_length': content_length,
                                'redirect': redirect
                            })
                        elif classify_as_directory(display_name, redirect):
                            directories.append({
                                'name': display_name,
                                'status_code': int(status_code),
                                'content_length': content_length,
                                'redirect': redirect
                            })
                        else:
                            routes.append({
                                'name': display_name,
                                'status_code': int(status_code),
                                'content_length': content_length,
                                'redirect': redirect
                            })
            # Старый формат с records_by_status
            elif 'records_by_status' in filtered_stats:
                for status_code, groups in filtered_stats.get('records_by_status', {}).items():
                    print(f"Обрабатываем статус {status_code}, групп: {len(groups)}")
                    for group in groups:
                        records = group.get('records', [])
                        print(f"В группе записей: {len(records)}")
                        for record in records:
                            # Правильно извлекаем FUZZ значение
                            fuzz_value = record.get('FUZZ', record.get('fuzz_value', record.get('url', '')))
                            url = record.get('url', '')
                            content_length = record.get('content_length', 0)
                            
                            # Извлекаем полный путь из URL
                            if url:
                                full_path = extract_path_from_url(url)
                                display_name = full_path if full_path else fuzz_value
                            else:
                                display_name = fuzz_value
                            
                            print(f"Обрабатываем запись: {display_name}, длина: {content_length}")
                            
                            # Классифицируем запись
                            redirect = record.get('redirectlocation', record.get('redirect', ''))
                            if classify_as_file(display_name):
                                files.append({
                                    'name': display_name,
                                    'status_code': int(status_code),
                                    'content_length': content_length,
                                    'redirect': redirect
                                })
                            elif classify_as_directory(display_name, redirect):
                                directories.append({
                                    'name': display_name,
                                    'status_code': int(status_code),
                                    'content_length': content_length,
                                    'redirect': redirect
                                })
                            else:
                                routes.append({
                                    'name': display_name,
                                    'status_code': int(status_code),
                                    'content_length': content_length,
                                    'redirect': redirect
                                })
        else:
            # Обычный режим с парсингом CSV
            if 'csv_data' not in data:
                return jsonify({'error': 'CSV данные не предоставлены'}), 400
            
            csv_text = data['csv_data']
            exclude_filters = data.get('exclude_filters', None)
            
            files, directories, routes = parse_fuzz_csv_text(csv_text, exclude_filters)
        
        # Отладочная информация
        print(f"Парсинг завершен: файлы={len(files)}, каталоги={len(directories)}, маршруты={len(routes)}")
        
        # Форматируем данные для сохранения с дополнительной информацией
        files_text = '\n'.join([f"{item['name']} (HTTP {item['status_code']}, {item['content_length']} bytes)" for item in files])
        directories_text = '\n'.join([f"{item['name']} (HTTP {item['status_code']}, {item['content_length']} bytes)" for item in directories])
        routes_text = '\n'.join([f"{item['name']} (HTTP {item['status_code']}, {item['content_length']} bytes)" for item in routes])
        
        # Дополняем существующие данные новыми (не перезаписываем)
        existing_files = website.files or ''
        existing_directories = website.directories or ''
        existing_routes = website.routes or ''
        
        # Функция для объединения данных без дублирования
        def merge_data_without_duplicates(existing_data, new_data):
            if not existing_data:
                return new_data
            if not new_data:
                return existing_data
                
            existing_lines = set(line.strip() for line in existing_data.split('\n') if line.strip())
            new_lines = [line.strip() for line in new_data.split('\n') if line.strip()]
            
            # Добавляем только новые записи (проверяем по имени файла/каталога/маршрута)
            unique_new_lines = []
            for new_line in new_lines:
                new_name = new_line.split(' ')[0].split('(')[0].strip()
                is_duplicate = False
                
                for existing_line in existing_lines:
                    existing_name = existing_line.split(' ')[0].split('(')[0].strip()
                    if new_name == existing_name:
                        is_duplicate = True
                        break
                        
                if not is_duplicate:
                    unique_new_lines.append(new_line)
            
            if unique_new_lines:
                return existing_data + '\n' + '\n'.join(unique_new_lines)
            else:
                return existing_data
        
        # Объединяем данные без дублирования и подсчитываем статистику
        new_files_data = merge_data_without_duplicates(existing_files, files_text)
        new_directories_data = merge_data_without_duplicates(existing_directories, directories_text)
        new_routes_data = merge_data_without_duplicates(existing_routes, routes_text)
        
        # Подсчитываем количество добавленных записей
        files_before = len([line for line in existing_files.split('\n') if line.strip()]) if existing_files else 0
        directories_before = len([line for line in existing_directories.split('\n') if line.strip()]) if existing_directories else 0
        routes_before = len([line for line in existing_routes.split('\n') if line.strip()]) if existing_routes else 0
        
        files_after = len([line for line in new_files_data.split('\n') if line.strip()]) if new_files_data else 0
        directories_after = len([line for line in new_directories_data.split('\n') if line.strip()]) if new_directories_data else 0
        routes_after = len([line for line in new_routes_data.split('\n') if line.strip()]) if new_routes_data else 0
        
        files_added = files_after - files_before
        directories_added = directories_after - directories_before
        routes_added = routes_after - routes_before
        
        files_duplicates = len(files) - files_added
        directories_duplicates = len(directories) - directories_added
        routes_duplicates = len(routes) - routes_added
        
        website.files = new_files_data
        website.directories = new_directories_data
        website.routes = new_routes_data
        
        db.session.commit()
        
        return jsonify({
            'message': f'Данные успешно импортированы. Добавлено: {files_added + directories_added + routes_added} новых записей, пропущено дубликатов: {files_duplicates + directories_duplicates + routes_duplicates}',
            'stats': {
                'files_count': files_after,
                'directories_count': directories_after,
                'routes_count': routes_after,
                'files_total': files_after,
                'directories_total': directories_after,
                'routes_total': routes_after,
                'files_added': files_added,
                'directories_added': directories_added,
                'routes_added': routes_added,
                'files_duplicates': files_duplicates,
                'directories_duplicates': directories_duplicates,
                'routes_duplicates': routes_duplicates
            },
            'debug': {
                'files_sample': files[:3] if files else [],
                'directories_sample': directories[:3] if directories else [],
                'routes_sample': routes[:3] if routes else []
            }
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка при импорте: {str(e)}'}), 500

@app.route('/api/websites/<int:website_id>/delete-entry', methods=['POST'])
def delete_entry(website_id):
    """Удаляет отдельную запись из файлов, каталогов или маршрутов"""
    website = Website.query.get_or_404(website_id)
    
    try:
        data = request.get_json()
        entry_type = data.get('entry_type')  # 'files', 'directories', 'routes'
        entry_name = data.get('entry_name')
        
        if not entry_type or not entry_name:
            return jsonify({'error': 'Тип записи и имя обязательны'}), 400
        
        # Получаем текущие данные
        if entry_type == 'files':
            current_data = website.files or ''
        elif entry_type == 'directories':
            current_data = website.directories or ''
        elif entry_type == 'routes':
            current_data = website.routes or ''
        else:
            return jsonify({'error': 'Неверный тип записи'}), 400
        
        # Разбиваем на строки и фильтруем
        lines = current_data.split('\n')
        filtered_lines = []
        
        for line in lines:
            if line.strip():
                # Извлекаем имя записи (до первого пробела или скобки)
                line_name = line.split(' ')[0].split('(')[0].strip()
                if line_name != entry_name:
                    filtered_lines.append(line)
        
        # Обновляем данные
        new_data = '\n'.join(filtered_lines)
        
        if entry_type == 'files':
            website.files = new_data
        elif entry_type == 'directories':
            website.directories = new_data
        elif entry_type == 'routes':
            website.routes = new_data
        
        db.session.commit()
        
        return jsonify({
            'message': f'Запись "{entry_name}" успешно удалена из {entry_type}',
            'updated_data': new_data
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка при удалении: {str(e)}'}), 500

@app.route('/api/functions/<int:function_id>', methods=['PUT'])
def update_function(function_id):
    """Обновляет функцию"""
    function = SiteFunction.query.get_or_404(function_id)
    
    try:
        data = request.get_json()
        
        # Обновляем поля функции
        if 'name' in data:
            function.name = data['name']
        if 'description' in data:
            function.description = data['description']
        if 'status' in data:
            function.status = data['status']
        if 'parent_id' in data:
            function.parent_id = data['parent_id']
        
        db.session.commit()
        
        return jsonify({
            'id': function.id,
            'name': function.name,
            'description': function.description,
            'status': function.status,
            'parent_id': function.parent_id,
            'message': 'Функция успешно обновлена'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка при обновлении функции: {str(e)}'}), 500

@app.route('/api/functions/<int:function_id>', methods=['DELETE'])
def delete_function(function_id):
    """Удаляет функцию и все связанные с ней данные"""
    function = SiteFunction.query.get_or_404(function_id)
    
    try:
        # Проверяем, есть ли дочерние функции
        children_count = SiteFunction.query.filter_by(parent_id=function_id).count()
        if children_count > 0:
            return jsonify({'error': 'Нельзя удалить функцию, у которой есть дочерние функции'}), 400
        
        # Удаляем функцию (связанные endpoints и notes удалятся автоматически благодаря cascade)
        db.session.delete(function)
        db.session.commit()
        
        return jsonify({'message': f'Функция "{function.name}" успешно удалена'})
        
    except Exception as e:
        return jsonify({'error': f'Ошибка при удалении функции: {str(e)}'}), 500

@app.route('/api/websites/<int:website_id>/transfer-data', methods=['POST'])
def transfer_data(website_id):
    """Переносит файлы, роуты и каталоги в URL любого функционала"""
    website = Website.query.get_or_404(website_id)
    
    try:
        data = request.get_json()
        target_function_id = data.get('target_function_id')
        transfer_files = data.get('transfer_files', [])
        transfer_routes = data.get('transfer_routes', [])
        transfer_directories = data.get('transfer_directories', [])
        
        if not target_function_id:
            return jsonify({'error': 'ID целевой функции обязателен'}), 400
        
        target_function = SiteFunction.query.get_or_404(target_function_id)
        
        # Создаем endpoints для переносимых данных
        created_endpoints = []
        
        # Переносим файлы
        for file_name in transfer_files:
            endpoint = Endpoint(
                url=f'{website.url.rstrip("/")}/{file_name}',
                method='GET',
                parameters='',
                headers='',
                response_info=f'Файл: {file_name}',
                function_id=target_function_id,
                status='Не начат'
            )
            db.session.add(endpoint)
            created_endpoints.append(f'Файл: {file_name}')
        
        # Переносим маршруты
        for route_name in transfer_routes:
            endpoint = Endpoint(
                url=f'{website.url.rstrip("/")}/{route_name}',
                method='GET',
                parameters='',
                headers='',
                response_info=f'Маршрут: {route_name}',
                function_id=target_function_id,
                status='Не начат'
            )
            db.session.add(endpoint)
            created_endpoints.append(f'Маршрут: {route_name}')
        
        # Переносим каталоги
        for dir_name in transfer_directories:
            endpoint = Endpoint(
                url=f'{website.url.rstrip("/")}/{dir_name}/',
                method='GET',
                parameters='',
                headers='',
                response_info=f'Каталог: {dir_name}',
                function_id=target_function_id,
                status='Не начат'
            )
            db.session.add(endpoint)
            created_endpoints.append(f'Каталог: {dir_name}')
        
        db.session.commit()
        
        return jsonify({
            'message': f'Успешно перенесено {len(created_endpoints)} элементов в функцию "{target_function.name}"',
            'created_endpoints': created_endpoints,
            'target_function': target_function.name
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка при переносе данных: {str(e)}'}), 500

@app.route('/api/websites/<int:website_id>/transfer-single-entry', methods=['POST'])
def transfer_single_entry(website_id):
    """Переносит одну запись (файл, маршрут или каталог) в указанную функцию"""
    website = Website.query.get_or_404(website_id)
    
    try:
        data = request.get_json()
        target_function_id = data.get('target_function_id')
        entry_type = data.get('entry_type')  # 'files', 'directories', 'routes'
        entry_name = data.get('entry_name')
        
        if not target_function_id or not entry_type or not entry_name:
            return jsonify({'error': 'ID функции, тип записи и имя записи обязательны'}), 400
        
        target_function = SiteFunction.query.get_or_404(target_function_id)
        
        # Создаем endpoint для переносимой записи
        if entry_type == 'files':
            endpoint = Endpoint(
                url=f'{website.url.rstrip("/")}/{entry_name}',
                method='GET',
                parameters='',
                headers='',
                response_info=f'Файл: {entry_name}',
                function_id=target_function_id,
                status='Не начат'
            )
        elif entry_type == 'routes':
            endpoint = Endpoint(
                url=f'{website.url.rstrip("/")}/{entry_name}',
                method='GET',
                parameters='',
                headers='',
                response_info=f'Маршрут: {entry_name}',
                function_id=target_function_id,
                status='Не начат'
            )
        elif entry_type == 'directories':
            endpoint = Endpoint(
                url=f'{website.url.rstrip("/")}/{entry_name}/',
                method='GET',
                parameters='',
                headers='',
                response_info=f'Каталог: {entry_name}',
                function_id=target_function_id,
                status='Не начат'
            )
        else:
            return jsonify({'error': 'Неверный тип записи'}), 400
        
        db.session.add(endpoint)
        
        # Удаляем запись из исходной таблицы
        if entry_type == 'files':
            current_data = website.files or ''
        elif entry_type == 'directories':
            current_data = website.directories or ''
        elif entry_type == 'routes':
            current_data = website.routes or ''
        
        # Разбиваем на строки и фильтруем
        lines = current_data.split('\n')
        filtered_lines = []
        
        for line in lines:
            if line.strip():
                # Извлекаем имя записи (до первого пробела или скобки)
                line_name = line.split(' ')[0].split('(')[0].strip()
                if line_name != entry_name:
                    filtered_lines.append(line)
        
        # Обновляем данные в исходной таблице
        updated_data = '\n'.join(filtered_lines)
        
        if entry_type == 'files':
            website.files = updated_data
        elif entry_type == 'directories':
            website.directories = updated_data
        elif entry_type == 'routes':
            website.routes = updated_data
        
        db.session.commit()
        
        return jsonify({
            'message': f'Запись "{entry_name}" успешно перенесена в функцию "{target_function.name}"',
            'created_endpoint': {
                'url': endpoint.url,
                'response_info': endpoint.response_info
            },
            'target_function': target_function.name,
            'updated_data': updated_data
        })
        
    except Exception as e:
        return jsonify({'error': f'Ошибка при переносе записи: {str(e)}'}), 500

@app.route('/api/websites/<int:website_id>/technologies', methods=['GET', 'POST'])
def api_technologies(website_id):
    """API для работы с технологиями сайта"""
    website = Website.query.get_or_404(website_id)
    
    if request.method == 'POST':
        data = request.get_json()
        technology = Technology(
            name=data['name'],
            version=data.get('version', ''),
            website_id=website_id
        )
        db.session.add(technology)
        db.session.commit()
        return jsonify({
            'id': technology.id,
            'name': technology.name,
            'version': technology.version,
            'message': 'Технология добавлена успешно'
        })
    
    technologies = Technology.query.filter_by(website_id=website_id).all()
    return jsonify([{
        'id': t.id,
        'name': t.name,
        'version': t.version,
        'created_at': t.created_at.isoformat()
    } for t in technologies])

@app.route('/api/websites/<int:website_id>/ports', methods=['GET', 'POST'])
def api_ports(website_id):
    """API для работы с портами сайта"""
    website = Website.query.get_or_404(website_id)
    
    if request.method == 'POST':
        data = request.get_json()
        port = Port(
            number=data['number'],
            service=data.get('service', ''),
            status=data.get('status', 'open'),
            website_id=website_id
        )
        db.session.add(port)
        db.session.commit()
        return jsonify({
            'id': port.id,
            'number': port.number,
            'service': port.service,
            'status': port.status,
            'message': 'Порт добавлен успешно'
        })
    
    ports = Port.query.filter_by(website_id=website_id).all()
    return jsonify([{
        'id': p.id,
        'number': p.number,
        'service': p.service,
        'status': p.status,
        'created_at': p.created_at.isoformat()
    } for p in ports])

@app.route('/api/technologies/<int:tech_id>', methods=['DELETE'])
def delete_technology(tech_id):
    """Удаление технологии"""
    technology = Technology.query.get_or_404(tech_id)
    db.session.delete(technology)
    db.session.commit()
    return jsonify({'message': 'Технология удалена успешно'})

@app.route('/api/ports/<int:port_id>', methods=['DELETE'])
def delete_port(port_id):
    """Удаление порта"""
    port = Port.query.get_or_404(port_id)
    db.session.delete(port)
    db.session.commit()
    return jsonify({'message': 'Порт удален успешно'})

@app.route('/api/websites/<int:website_id>/upload-nmap-ports', methods=['POST'])
def upload_nmap_ports(website_id):
    """Загрузка портов из nmap XML файла"""
    website = Website.query.get_or_404(website_id)
    
    try:
        data = request.get_json()
        ports_data = data.get('ports', [])
        
        if not ports_data:
            return jsonify({'error': 'Нет данных о портах'}), 400
        
        added_count = 0
        skipped_count = 0
        
        for port_info in ports_data:
            port_number = port_info.get('number')
            service = port_info.get('service', '')
            status = port_info.get('status', 'open')
            
            # Проверяем, не существует ли уже такой порт
            existing_port = Port.query.filter_by(
                website_id=website_id, 
                number=port_number
            ).first()
            
            if existing_port:
                # Обновляем существующий порт, если сервис не указан
                if not existing_port.service and service:
                    existing_port.service = service
                    existing_port.status = status
                    added_count += 1
                else:
                    skipped_count += 1
            else:
                # Создаем новый порт
                new_port = Port(
                    number=port_number,
                    service=service,
                    status=status,
                    website_id=website_id
                )
                db.session.add(new_port)
                added_count += 1
        
        db.session.commit()
        
        return jsonify({
            'message': 'Порты успешно загружены',
            'added_count': added_count,
            'skipped_count': skipped_count
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка при загрузке портов: {str(e)}'}), 500

@app.route('/api/websites/<int:website_id>/migrate-data', methods=['POST'])
def migrate_website_data(website_id):
    """Миграция данных из старых текстовых полей в новые таблицы"""
    website = Website.query.get_or_404(website_id)
    
    try:
        migrated_count = 0
        
        # Миграция технологий
        if website.technologies:
            tech_list = [tech.strip() for tech in website.technologies.split(',') if tech.strip()]
            for tech_entry in tech_list:
                # Разделяем строку на отдельные технологии (например, "Laravel Apache 2.40.2 Nginx")
                individual_techs = parse_multiple_technologies(tech_entry)
                
                for tech_name, tech_version in individual_techs:
                    # Проверяем, не существует ли уже такая технология
                    existing = Technology.query.filter_by(website_id=website_id, name=tech_name, version=tech_version).first()
                    if not existing:
                        technology = Technology(name=tech_name, version=tech_version, website_id=website_id)
                        db.session.add(technology)
                        migrated_count += 1
        
        # Миграция портов
        if website.ports:
            # Поддерживаем как разделение запятыми, так и переносами строк
            if ',' in website.ports:
                port_list = [port.strip() for port in website.ports.split(',') if port.strip()]
            else:
                port_list = [port.strip() for port in website.ports.split('\n') if port.strip()]
            for port_str in port_list:
                try:
                    # Извлекаем номер порта (может быть в формате "80", "80/tcp", "80 (http)")
                    port_number = int(port_str.split('/')[0].split('(')[0].strip())
                    
                    # Извлекаем сервис, если указан
                    service = ''
                    if '(' in port_str and ')' in port_str:
                        service = port_str.split('(')[1].split(')')[0].strip()
                    
                    # Проверяем, не существует ли уже такой порт
                    existing = Port.query.filter_by(website_id=website_id, number=port_number).first()
                    if not existing:
                        port = Port(number=port_number, service=service, website_id=website_id)
                        db.session.add(port)
                        migrated_count += 1
                except ValueError:
                    # Пропускаем некорректные номера портов
                    continue
        
        db.session.commit()
        
        return jsonify({
            'message': f'Миграция завершена. Перенесено {migrated_count} записей.',
            'migrated_count': migrated_count
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка при миграции: {str(e)}'}), 500

@app.route('/api/debug/database', methods=['GET'])
def debug_database():
    """Диагностика состояния базы данных"""
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        
        debug_info = {
            'database_uri': app.config['SQLALCHEMY_DATABASE_URI'],
            'tables': tables,
            'table_counts': {},
            'sample_data': {}
        }
        
        # Подсчитываем записи в каждой таблице
        for table in tables:
            try:
                if table == 'website':
                    count = Website.query.count()
                    debug_info['table_counts'][table] = count
                    if count > 0:
                        sample = Website.query.first()
                        debug_info['sample_data'][table] = {
                            'id': sample.id,
                            'name': sample.name,
                            'url': sample.url
                        }
                elif table == 'technology':
                    count = Technology.query.count()
                    debug_info['table_counts'][table] = count
                    if count > 0:
                        sample = Technology.query.first()
                        debug_info['sample_data'][table] = {
                            'id': sample.id,
                            'name': sample.name,
                            'version': sample.version,
                            'website_id': sample.website_id
                        }
                elif table == 'port':
                    count = Port.query.count()
                    debug_info['table_counts'][table] = count
                    if count > 0:
                        sample = Port.query.first()
                        debug_info['sample_data'][table] = {
                            'id': sample.id,
                            'number': sample.number,
                            'service': sample.service,
                            'website_id': sample.website_id
                        }
                else:
                    # Для других таблиц используем прямой SQL
                    with db.engine.connect() as conn:
                        result = conn.execute(text(f'SELECT COUNT(*) FROM {table}'))
                        count = result.scalar()
                        debug_info['table_counts'][table] = count
            except Exception as e:
                debug_info['table_counts'][table] = f'Ошибка: {str(e)}'
        
        # Проверяем данные по сайтам
        websites = Website.query.all()
        debug_info['websites_detail'] = []
        for website in websites:
            tech_count = Technology.query.filter_by(website_id=website.id).count()
            port_count = Port.query.filter_by(website_id=website.id).count()
            debug_info['websites_detail'].append({
                'id': website.id,
                'name': website.name,
                'url': website.url,
                'technologies_count': tech_count,
                'ports_count': port_count
            })
        
        return jsonify(debug_info)
        
    except Exception as e:
        return jsonify({'error': f'Ошибка диагностики: {str(e)}'}), 500

# Инициализируем базу данных
init_db()

def test_csv_import_logic():
    """Тестирует логику импорта CSV данных"""
    print("\n=== Тест логики импорта CSV ===")
    
    # Тестовые данные
    csv_data = """FUZZ,url,status_code,content_length,content_lines,content_words,redirectlocation
render/https://www.google.com,https://edu.burgerkingrus.ru/admin/render/https://www.google.com,200,1234,50,200,
wp-admin/,https://edu.burgerkingrus.ru/admin/wp-admin/,302,567,20,100,https://edu.burgerkingrus.ru/admin/wp-admin/login.php"""
    
    try:
        files, directories, routes = parse_fuzz_csv_text(csv_data)
        print(f"✓ Парсинг выполнен: {len(files)} файлов, {len(directories)} каталогов, {len(routes)} маршрутов")
        
        # Проверяем что есть записи с правильными путями
        all_records = files + directories + routes
        found_admin_render = False
        found_admin_wp = False
        
        print("Найденные записи:")
        for record in all_records:
            name = record['name']
            print(f"  - {name}")
            if 'admin/render/https://www.google.com' in name:
                found_admin_render = True
            if 'admin/wp-admin/' in name:
                found_admin_wp = True
        
        if found_admin_render and found_admin_wp:
            print("✓ ТЕСТ ПРОЙДЕН: Полные пути правильно извлечены из URL")
            return True
        else:
            print("✗ ТЕСТ НЕ ПРОЙДЕН: Некоторые пути не найдены")
            return False
            
    except Exception as e:
        print(f"✗ Ошибка при тестировании: {e}")
        import traceback
        traceback.print_exc()
        return False

def create_ssl_context():
    """Создает SSL контекст для HTTPS"""
    import ssl
    import os
    
    # Проверяем наличие сертификатов
    cert_file = 'cert.pem'
    key_file = 'key.pem'
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        try:
            # Используем более современный способ создания SSL контекста
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            context.load_cert_chain(cert_file, key_file)
            return context
        except Exception as e:
            print(f"Ошибка при загрузке SSL сертификата: {e}")
            print("Запуск без HTTPS...")
            return None
    else:
        print("SSL сертификаты не найдены. Создайте их с помощью generate_cert.py")
        return None

if __name__ == '__main__':
    # Запускаем тест логики импорта CSV
    test_result = test_csv_import_logic()
    if not test_result:
        print("ВНИМАНИЕ: Тест логики импорта не пройден!")
    
    # Временно запускаем только с HTTP для тестирования
    port = int(os.environ.get('PORT', '5000'))
    print(f"Запуск сервера с HTTP на http://localhost:{port}")
    print("Функция копирования портов будет работать с альтернативным методом")
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=False, threaded=True)
    
    # Для HTTPS раскомментируйте код ниже:
    # ssl_context = create_ssl_context()
    # if ssl_context:
    #     print("Запуск сервера с HTTPS на https://localhost:5000")
    #     print("Внимание: Используется самоподписанный сертификат")
    #     app.run(debug=True, host='0.0.0.0', port=5000, ssl_context=ssl_context)
    # else:
    #     print("Запуск сервера с HTTP на http://localhost:5000")
    #     print("Для HTTPS создайте сертификаты: python generate_cert.py")
    #     app.run(debug=True, host='0.0.0.0', port=5000)
SCREENSHOT_SEM = threading.BoundedSemaphore(4)
