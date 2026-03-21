#!/usr/bin/env python3
"""
PDF → MP3 Web 应用（修复版）
修复：超时保护 / 分段合成 / 逐页进度日志 / 自动重试
"""

import asyncio, os, re, threading, uuid, time, tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

tasks = {}  # task_id -> {logs, done, success, mp3_path}

# ═══════════════════════════════════════════════════════════════
#  文本处理
# ═══════════════════════════════════════════════════════════════

def has_chinese(text):
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))

def clean_mixed_line(line):
    line = re.sub(r'(?<![^\s])[A-Za-z][\w\'\-]*(?:\s+[A-Za-z][\w\'\-]*){3,}[.!?,;:)]*', ' ', line)
    line = re.sub(r'\b(True|False|None|return|import|from|def|class)\b.*', '', line)
    return re.sub(r' {2,}', ' ', line).strip()

def process_page_text(raw):
    kept = []
    for para in re.split(r'\n{2,}', raw):
        para = para.strip()
        if not para or not has_chinese(para):
            continue
        cleaned_lines = []
        for line in para.split('\n'):
            line = line.strip()
            if not line: continue
            if not has_chinese(line):
                if len(re.findall(r'\b[a-zA-Z]{2,}\b', line)) >= 3: continue
                if re.search(r'[=\(\)\[\]\{\}#]', line): continue
                if len(re.findall(r'[a-zA-Z]', line)) > len(line) * 0.5: continue
            c = clean_mixed_line(line)
            if c and has_chinese(c):
                cleaned_lines.append(c)
        if cleaned_lines:
            kept.append('\n'.join(cleaned_lines))
    return '\n\n'.join(kept)

def post_process(text):
    out = []
    for line in text.split('\n'):
        line = line.strip()
        if re.match(r'^\d+$', line): continue
        if line and not has_chinese(line):
            if len(re.findall(r'\b[a-zA-Z]{3,}\b', line)) >= 2: continue
            if re.match(r'^[a-zA-Z0-9=\s\.\,\(\)\[\]\_\-\+\*\/\#\:\"\']{1,60}$', line): continue
        out.append(line)
    text = '\n'.join(out)
    text = re.sub(r'https?://\S+', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()

def merge_lines_for_tts(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    result = []
    for para in re.split(r'\n{2,}', text):
        para = para.strip()
        if not para:
            continue
        lines = para.split('\n')
        merged = ''
        for i, line in enumerate(lines):
            line = line.strip()
            if not line: continue
            if i == 0:
                merged = line
            else:
                last_char  = merged[-1] if merged else ''
                first_char = line[0] if line else ''
                is_last_cn  = '\u4e00' <= last_char  <= '\u9fff' or last_char  in '，。！？、：；…）】」'
                is_first_cn = '\u4e00' <= first_char <= '\u9fff' or first_char in '（【「'
                if is_last_cn or is_first_cn:
                    merged += line
                else:
                    merged += ' ' + line
        if merged:
            result.append(merged)
    return '\n'.join(result)

def extract_text_from_pdf(pdf_path, start_page, end_page, log_fn):
    import pdfplumber
    log_fn(f"📖 正在读取第 {start_page}–{end_page} 页…")
    all_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        log_fn(f"   PDF 共 {total} 页，开始提取文字…")
        end = min(end_page, total)
        page_count = end - start_page + 1
        for i, idx in enumerate(range(start_page - 1, end)):
            raw = pdf.pages[idx].extract_text()
            if raw:
                cleaned = process_page_text(raw)
                if cleaned:
                    all_parts.append(cleaned)
            # 每页都记录进度
            log_fn(f"   第 {start_page + i} 页提取完成 ({i+1}/{page_count})")
    if not all_parts:
        return None
    text = post_process('\n\n'.join(all_parts))
    cn = len(re.findall(r'[\u4e00-\u9fff]', text))
    log_fn(f"✅ 文本提取完成：{len(text)} 字符，中文 {cn} 字")
    return text

# ═══════════════════════════════════════════════════════════════
#  TTS：分段合成 + 超时保护 + 自动重试
# ═══════════════════════════════════════════════════════════════

CHUNK_SIZE = 800  # 每段最大字符数

def split_text_into_chunks(text, max_chars=CHUNK_SIZE):
    """按句末标点切分，每段不超过 max_chars"""
    sentences = re.split(r'(?<=[。！？\.\!\?])', text)
    chunks = []
    current = ""
    for s in sentences:
        if not s.strip():
            continue
        if len(current) + len(s) <= max_chars:
            current += s
        else:
            if current:
                chunks.append(current.strip())
            while len(s) > max_chars:
                chunks.append(s[:max_chars])
                s = s[max_chars:]
            current = s
    if current.strip():
        chunks.append(current.strip())
    return chunks

async def _tts_one_chunk(text, voice, rate, output_path, timeout=90):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await asyncio.wait_for(communicate.save(output_path), timeout=timeout)

async def _tts_all_chunks(chunks, voice, rate, tmp_dir, log_fn):
    paths = []
    for i, chunk in enumerate(chunks):
        out = os.path.join(tmp_dir, f"chunk_{i:04d}.mp3")
        success = False
        for attempt in range(2):
            try:
                log_fn(f"   🔊 合成第 {i+1}/{len(chunks)} 段…")
                await _tts_one_chunk(chunk, voice, rate, out, timeout=90)
                paths.append(out)
                success = True
                break
            except asyncio.TimeoutError:
                if attempt == 0:
                    log_fn(f"   ⚠️ 第 {i+1} 段超时，2秒后重试…")
                    await asyncio.sleep(2)
                else:
                    log_fn(f"   ❌ 第 {i+1} 段超时失败，已跳过")
            except Exception as e:
                if attempt == 0:
                    log_fn(f"   ⚠️ 第 {i+1} 段出错（{e}），3秒后重试…")
                    await asyncio.sleep(3)
                else:
                    log_fn(f"   ❌ 第 {i+1} 段失败（{e}），已跳过")
    return paths

def merge_mp3_files(chunk_paths, output_path):
    """直接拼接 mp3 二进制，无需 ffmpeg"""
    with open(output_path, 'wb') as out:
        for p in chunk_paths:
            with open(p, 'rb') as f:
                out.write(f.read())

def generate_mp3(text, voice, rate, output_path, log_fn):
    log_fn(f"🎙 声音：{voice}  语速：{rate}")
    tts_text = merge_lines_for_tts(text)
    chunks = split_text_into_chunks(tts_text)
    log_fn(f"📝 文本已分为 {len(chunks)} 段，开始逐段合成…")

    with tempfile.TemporaryDirectory() as tmp_dir:
        chunk_paths = asyncio.run(
            _tts_all_chunks(chunks, voice, rate, tmp_dir, log_fn)
        )
        if not chunk_paths:
            raise RuntimeError("所有段均合成失败，请检查网络连接或更换语音")
        log_fn(f"🔗 正在合并 {len(chunk_paths)} 段音频…")
        merge_mp3_files(chunk_paths, output_path)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log_fn(f"✅ MP3 完成，大小：{size_mb:.1f} MB")
    return True

# ═══════════════════════════════════════════════════════════════
#  后台任务
# ═══════════════════════════════════════════════════════════════

def run_task(task_id, pdf_path, start, end, voice, rate):
    task = tasks[task_id]

    def log(msg):
        task["logs"].append(msg)

    try:
        text = extract_text_from_pdf(pdf_path, start, end, log)
        if not text:
            log("❌ 未提取到中文内容，请检查页码范围或 PDF 是否为扫描版（图片）")
            task["done"] = True
            return

        mp3_path = str(OUTPUT_DIR / f"{task_id}.mp3")
        generate_mp3(text, voice, rate, mp3_path, log)
        task["mp3_path"] = mp3_path
        task["success"] = True
        log("🎉 全部完成！点击下方按钮下载 MP3")

    except Exception as e:
        import traceback
        log(f"❌ 出错：{e}")
        log(traceback.format_exc())

    finally:
        task["done"] = True
        try: os.remove(pdf_path)
        except: pass

# ═══════════════════════════════════════════════════════════════
#  路由
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "pdf" not in request.files:
        return jsonify({"error": "没有收到文件"}), 400

    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "请上传 PDF 文件"}), 400

    try:
        start = int(request.form.get("start", 1))
        end   = int(request.form.get("end", 50))
        voice = request.form.get("voice", "zh-CN-XiaoxiaoNeural")
        rate  = request.form.get("rate", "+0%")
    except ValueError:
        return jsonify({"error": "页码必须是整数"}), 400

    if start < 1 or end < start:
        return jsonify({"error": "页码范围无效"}), 400

    task_id  = uuid.uuid4().hex
    pdf_path = str(UPLOAD_DIR / f"{task_id}.pdf")
    f.save(pdf_path)

    tasks[task_id] = {
        "logs": [], "done": False,
        "success": False, "mp3_path": None
    }

    threading.Thread(
        target=run_task,
        args=(task_id, pdf_path, start, end, voice, rate),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id})

@app.route("/status/<task_id>")
def status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    from_idx = int(request.args.get("from", 0))
    return jsonify({
        "logs":    task["logs"][from_idx:],
        "done":    task["done"],
        "success": task["success"],
    })

@app.route("/download/<task_id>")
def download(task_id):
    task = tasks.get(task_id)
    if not task or not task.get("mp3_path"):
        return "文件不存在", 404

    mp3_path = task["mp3_path"]
    if not os.path.exists(mp3_path):
        return "文件已过期", 404

    return send_file(
        mp3_path,
        as_attachment=True,
        download_name="output.mp3",
        mimetype="audio/mpeg"
    )

if __name__ == "__main__":
    print("🚀 启动服务：http://127.0.0.1:5000")
    app.run(debug=False, host="0.0.0.0", port=5000)
