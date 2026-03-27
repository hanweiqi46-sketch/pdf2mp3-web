#!/usr/bin/env python3
"""
PDF → MP3 Web 应用（修复版）
修复：超时保护 / 分段合成 / 逐页进度日志 / 自动重试
"""

import asyncio, os, re, threading, uuid, time, tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

# 初始化 Flask 应用
app = Flask(__name__)

# 定义上传和输出目录
UPLOAD_DIR = Path("uploads")  # PDF 文件上传目录
OUTPUT_DIR = Path("outputs")  # MP3 文件输出目录
UPLOAD_DIR.mkdir(exist_ok=True)  # 确保目录存在
OUTPUT_DIR.mkdir(exist_ok=True)

# 全局任务字典，存储每个任务的状态
# 结构：task_id -> {logs: [], done: bool, success: bool, mp3_path: str}
tasks = {}

# ═══════════════════════════════════════════════════════════════
#  文本处理
# ═══════════════════════════════════════════════════════════════

def has_chinese(text):
    """
    检测文本中是否包含中文字符
    参数：text - 待检测的文本
    返回：True 如果包含中文，否则 False
    """
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))

def clean_mixed_line(line):
    """
    清理混合中英文的行，移除纯英文片段和代码关键字
    参数：line - 待清理的文本行
    返回：清理后的文本
    """
    # 移除连续的英文单词（4个或以上）
    line = re.sub(r'(?<![^\s])[A-Za-z][\w\'\-]*(?:\s+[A-Za-z][\w\'\-]*){3,}[.!?,;:)]*', ' ', line)
    # 移除 Python 关键字及其后续内容
    line = re.sub(r'\b(True|False|None|return|import|from|def|class)\b.*', '', line)
    # 压缩多余空格
    return re.sub(r' {2,}', ' ', line).strip()

def process_page_text(raw):
    """
    处理 PDF 页面的原始文本，过滤掉非中文内容和代码片段
    参数：raw - PDF 页面的原始文本
    返回：清理后的文本段落
    """
    kept = []
    # 按空行分割段落
    for para in re.split(r'\n{2,}', raw):
        para = para.strip()
        # 跳过空段落或不含中文的段落
        if not para or not has_chinese(para):
            continue
        cleaned_lines = []
        for line in para.split('\n'):
            line = line.strip()
            if not line: continue
            # 如果行不含中文，进行额外过滤
            if not has_chinese(line):
                # 跳过包含 3 个以上英文单词的行
                if len(re.findall(r'\b[a-zA-Z]{2,}\b', line)) >= 3: continue
                # 跳过包含代码符号的行
                if re.search(r'[=\(\)\[\]\{\}#]', line): continue
                # 跳过英文字符超过 50% 的行
                if len(re.findall(r'[a-zA-Z]', line)) > len(line) * 0.5: continue
            c = clean_mixed_line(line)
            if c and has_chinese(c):
                cleaned_lines.append(c)
        if cleaned_lines:
            kept.append('\n'.join(cleaned_lines))
    return '\n\n'.join(kept)

def post_process(text):
    """
    后处理文本，移除页码、纯英文行、URL 等无关内容
    参数：text - 待处理的文本
    返回：清理后的文本
    """
    out = []
    for line in text.split('\n'):
        line = line.strip()
        # 跳过纯数字行（通常是页码）
        if re.match(r'^\d+$', line): continue
        # 跳过不含中文的特定行
        if line and not has_chinese(line):
            # 跳过包含 2 个以上长英文单词的行
            if len(re.findall(r'\b[a-zA-Z]{3,}\b', line)) >= 2: continue
            # 跳过代码或配置行
            if re.match(r'^[a-zA-Z0-9=\s\.\,\(\)\[\]\_\-\+\*\/\#\:\"\']{1,60}$', line): continue
        out.append(line)
    text = '\n'.join(out)
    # 移除 URL
    text = re.sub(r'https?://\S+', '', text)
    # 压缩多余空行
    return re.sub(r'\n{3,}', '\n\n', text).strip()

def merge_lines_for_tts(text):
    """
    合并文本行以优化 TTS 朗读效果
    换行前有空格则停顿，无空格则连续读
    参数：text - 待处理的文本
    返回：合并后的文本
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    result = []
    # 按段落分割
    for para in re.split(r'\n{2,}', text):
        para = para.strip()
        if not para:
            continue
        lines = para.split('\n')
        merged = ''
        for i, line in enumerate(lines):
            if not line: continue
            if i == 0:
                merged = line.rstrip()
            else:
                # 检查上一行末尾是否有空格
                if merged and merged[-1] == ' ':
                    # 有空格：保持停顿（换行）
                    result.append(merged.rstrip())
                    merged = line.rstrip()
                else:
                    # 无空格：连续读（直接拼接）
                    merged += line.rstrip()
        if merged:
            result.append(merged)
    return '\n'.join(result)

def extract_text_from_pdf(pdf_path, start_page, end_page, log_fn):
    """
    从 PDF 文件中提取指定页码范围的文本
    参数：
        pdf_path - PDF 文件路径
        start_page - 起始页码（从 1 开始）
        end_page - 结束页码
        log_fn - 日志记录函数
    返回：提取并清理后的文本，如果没有内容则返回 None
    """
    import pdfplumber
    log_fn(f"📖 正在读取第 {start_page}–{end_page} 页…")
    all_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        log_fn(f"   PDF 共 {total} 页，开始提取文字…")
        end = min(end_page, total)
        page_count = end - start_page + 1
        # 逐页提取文本
        for i, idx in enumerate(range(start_page - 1, end)):
            raw = pdf.pages[idx].extract_text()
            if raw:
                cleaned = process_page_text(raw)
                if cleaned:
                    all_parts.append(cleaned)
            # 记录每页的提取进度
            log_fn(f"   第 {start_page + i} 页提取完成 ({i+1}/{page_count})")
    if not all_parts:
        return None
    # 合并所有页面并进行后处理
    text = post_process('\n\n'.join(all_parts))
    cn = len(re.findall(r'[\u4e00-\u9fff]', text))
    log_fn(f"✅ 文本提取完成：{len(text)} 字符，中文 {cn} 字")
    return text

# ═══════════════════════════════════════════════════════════════
#  TTS：分段合成 + 超时保护 + 自动重试
# ═══════════════════════════════════════════════════════════════

CHUNK_SIZE = 800  # 每段最大字符数，避免单次合成超时

def split_text_into_chunks(text, max_chars=CHUNK_SIZE):
    """
    按句末标点切分文本，每段不超过 max_chars
    参数：
        text - 待切分的文本
        max_chars - 每段最大字符数
    返回：文本段落列表
    """
    # 按句末标点分割
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
            # 如果单句超长，强制切分
            while len(s) > max_chars:
                chunks.append(s[:max_chars])
                s = s[max_chars:]
            current = s
    if current.strip():
        chunks.append(current.strip())
    return chunks

async def _tts_one_chunk(text, voice, rate, output_path, timeout=90):
    """
    异步合成单个文本段落为 MP3
    参数：
        text - 待合成的文本
        voice - 语音模型
        rate - 语速
        output_path - 输出文件路径
        timeout - 超时时间（秒）
    """
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await asyncio.wait_for(communicate.save(output_path), timeout=timeout)

async def _tts_all_chunks(chunks, voice, rate, tmp_dir, log_fn):
    """
    异步合成所有文本段落，带超时保护和自动重试，支持并发
    参数：
        chunks - 文本段落列表
        voice - 语音模型
        rate - 语速
        tmp_dir - 临时文件目录
        log_fn - 日志记录函数
    返回：成功合成的 MP3 文件路径列表
    """
    CONCURRENT = 3  # 并发数

    async def process_chunk(i, chunk):
        out = os.path.join(tmp_dir, f"chunk_{i:04d}.mp3")
        for attempt in range(2):
            try:
                log_fn(f"   🔊 合成第 {i+1}/{len(chunks)} 段…")
                await _tts_one_chunk(chunk, voice, rate, out, timeout=90)
                return (i, out)
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
        return (i, None)

    results = []
    for batch_start in range(0, len(chunks), CONCURRENT):
        batch = chunks[batch_start:batch_start + CONCURRENT]
        tasks = [process_chunk(batch_start + j, chunk) for j, chunk in enumerate(batch)]
        results.extend(await asyncio.gather(*tasks))

    return [path for _, path in sorted(results) if path]

def merge_mp3_files(chunk_paths, output_path):
    """
    直接拼接 MP3 二进制文件，无需 ffmpeg
    参数：
        chunk_paths - MP3 文件路径列表
        output_path - 输出文件路径
    """
    with open(output_path, 'wb') as out:
        for p in chunk_paths:
            with open(p, 'rb') as f:
                out.write(f.read())

def generate_mp3(text, voice, rate, output_path, log_fn):
    """
    生成 MP3 音频文件的主函数
    参数：
        text - 待合成的文本
        voice - 语音模型
        rate - 语速
        output_path - 输出文件路径
        log_fn - 日志记录函数
    返回：True 表示成功
    """
    log_fn(f"🎙 声音：{voice}  语速：{rate}")
    # 优化文本格式以适配 TTS
    tts_text = merge_lines_for_tts(text)
    # 分段处理
    chunks = split_text_into_chunks(tts_text)
    log_fn(f"📝 文本已分为 {len(chunks)} 段，开始逐段合成…")

    # 使用临时目录存储分段音频
    with tempfile.TemporaryDirectory() as tmp_dir:
        chunk_paths = asyncio.run(
            _tts_all_chunks(chunks, voice, rate, tmp_dir, log_fn)
        )
        if not chunk_paths:
            raise RuntimeError("所有段均合成失败，请检查网络连接或更换语音")
        success_rate = len(chunk_paths) / len(chunks)
        if success_rate < 0.8:
            raise RuntimeError(f"合成成功率过低 ({success_rate:.0%})，请重试或检查网络")
        log_fn(f"🔗 正在合并 {len(chunk_paths)}/{len(chunks)} 段音频…")
        merge_mp3_files(chunk_paths, output_path)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log_fn(f"✅ MP3 完成，大小：{size_mb:.1f} MB")
    return True

# ═══════════════════════════════════════════════════════════════
#  后台任务
# ═══════════════════════════════════════════════════════════════

def run_task(task_id, pdf_path, start, end, voice, rate):
    """
    后台任务：提取 PDF 文本并生成 MP3
    参数：
        task_id - 任务 ID
        pdf_path - PDF 文件路径
        start - 起始页码
        end - 结束页码
        voice - 语音模型
        rate - 语速
    """
    task = tasks[task_id]

    def log(msg):
        """将日志消息添加到任务日志列表"""
        task["logs"].append(msg)

    try:
        # 步骤 1：提取 PDF 文本
        text = extract_text_from_pdf(pdf_path, start, end, log)
        if not text:
            log("❌ 未提取到中文内容，请检查页码范围或 PDF 是否为扫描版（图片）")
            task["done"] = True
            return

        # 步骤 2：生成 MP3 音频
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
        # 标记任务完成并清理临时文件
        task["done"] = True
        try: os.remove(pdf_path)
        except: pass

# ═══════════════════════════════════════════════════════════════
#  路由
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    """首页路由，返回 HTML 页面"""
    return render_template("index.html")

@app.route("/health")
def health():
    """健康检查端点，用于保持服务活跃"""
    return jsonify({"status": "ok", "time": time.time()})

@app.route("/upload", methods=["POST"])
def upload():
    """
    文件上传路由，接收 PDF 文件和参数，创建后台任务
    返回：JSON 格式的任务 ID 或错误信息
    """
    # 验证文件是否存在
    if "pdf" not in request.files:
        return jsonify({"error": "没有收到文件"}), 400

    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "请上传 PDF 文件"}), 400

    # 解析请求参数
    try:
        start = int(request.form.get("start", 1))
        end   = int(request.form.get("end", 50))
        voice = request.form.get("voice", "zh-CN-XiaoxiaoNeural")
        rate  = request.form.get("rate", "+0%")
    except ValueError:
        return jsonify({"error": "页码必须是整数"}), 400

    # 验证页码范围
    if start < 1 or end < start:
        return jsonify({"error": "页码范围无效"}), 400

    # 保存上传的文件
    task_id  = uuid.uuid4().hex
    pdf_path = str(UPLOAD_DIR / f"{task_id}.pdf")
    f.save(pdf_path)

    # 初始化任务状态
    tasks[task_id] = {
        "logs": [], "done": False,
        "success": False, "mp3_path": None
    }

    # 启动后台线程处理任务
    threading.Thread(
        target=run_task,
        args=(task_id, pdf_path, start, end, voice, rate),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id})

@app.route("/status/<task_id>")
def status(task_id):
    """
    任务状态查询路由，返回任务的日志和完成状态
    参数：task_id - 任务 ID
    返回：JSON 格式的任务状态信息
    """
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    # 支持增量获取日志（从指定索引开始）
    from_idx = int(request.args.get("from", 0))
    return jsonify({
        "logs":    task["logs"][from_idx:],
        "done":    task["done"],
        "success": task["success"],
    })

@app.route("/download/<task_id>")
def download(task_id):
    """
    文件下载路由，提供生成的 MP3 文件下载
    参数：task_id - 任务 ID
    返回：MP3 文件或错误信息
    """
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
    # 启动 Flask 应用，监听所有网络接口
    app.run(debug=False, host="0.0.0.0", port=5000)
