#!/usr/bin/env python3
"""
PDF → MP3 Web 应用（修复版）
修复：超时保护 / 分段合成 / 逐页进度日志 / 自动重试
"""

import asyncio, os, re, threading, uuid, time, tempfile, subprocess, zipfile, io, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_file, render_template
from watermark import remove_watermark
from image_watermark import remove_image_watermark

# 初始化 Flask 应用
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 最大 2 GB 上传

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "文件过大（最大 2 GB）"}), 413

# 定义上传和输出目录
UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
VIDEO_OUT_DIR = Path("video_outputs")
SUBTITLE_OUT_DIR = Path("subtitle_outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
VIDEO_OUT_DIR.mkdir(exist_ok=True)
SUBTITLE_OUT_DIR.mkdir(exist_ok=True)

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

VIDEO_EXTS = {
    ".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".rmvb",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus", ".amr",
}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus", ".amr"}
FONT_SIZES = {"small": 16, "medium": 24, "large": 36}

# 全局任务字典
tasks = {}           # PDF→MP3 / 去水印任务
video_tasks = {}     # 视频转录任务
subtitle_tasks = {}  # 字幕任务

# Whisper 模型单例（懒加载）
_whisper_model = None
_whisper_lock = threading.Lock()

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

def run_watermark_task(task_id, pdf_path):
    """后台去水印任务"""
    task = tasks[task_id]

    def log(msg):
        task["logs"].append(msg)

    try:
        out_path = str(OUTPUT_DIR / f"{task_id}_nowm.pdf")
        remove_watermark(pdf_path, out_path, log)
        task["output_path"] = out_path
        task["success"] = True
        log("🎉 处理完成！点击下方按钮下载去水印 PDF")
    except Exception as e:
        import traceback
        log(f"❌ 出错：{e}")
        log(traceback.format_exc())
    finally:
        task["done"] = True
        try: os.remove(pdf_path)
        except: pass


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

@app.route("/upload/watermark", methods=["POST"])
def upload_watermark():
    """去水印上传路由"""
    if "pdf" not in request.files:
        return jsonify({"error": "没有收到文件"}), 400
    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "请上传 PDF 文件"}), 400

    task_id  = uuid.uuid4().hex
    pdf_path = str(UPLOAD_DIR / f"{task_id}.pdf")
    f.save(pdf_path)

    tasks[task_id] = {
        "logs": [], "done": False,
        "success": False, "output_path": None
    }

    threading.Thread(
        target=run_watermark_task,
        args=(task_id, pdf_path),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id})


@app.route("/download/watermark/<task_id>")
def download_watermark(task_id):
    """下载去水印后的 PDF"""
    task = tasks.get(task_id)
    if not task or not task.get("output_path"):
        return "文件不存在", 404
    out = task["output_path"]
    if not os.path.exists(out):
        return "文件已过期", 404
    return send_file(
        out,
        as_attachment=True,
        download_name="nowatermark.pdf",
        mimetype="application/pdf"
    )


@app.route("/upload/image-watermark", methods=["POST"])
def upload_image_watermark():
    """图片去水印：上传图片 + 区域坐标，返回处理后图片"""
    if "image" not in request.files:
        return jsonify({"error": "没有收到文件"}), 400
    f = request.files["image"]
    ext = os.path.splitext(f.filename.lower())[1]
    if ext not in ALLOWED_IMAGE_EXTS:
        return jsonify({"error": "请上传 JPG / PNG / WebP / BMP 图片"}), 400

    import json
    try:
        regions = json.loads(request.form.get("regions", "[]"))
    except Exception:
        return jsonify({"error": "区域格式错误"}), 400

    if not regions:
        return jsonify({"error": "请先在图片上框选水印区域"}), 400

    task_id    = uuid.uuid4().hex
    input_path = str(UPLOAD_DIR / f"{task_id}_in{ext}")
    output_path = str(OUTPUT_DIR / f"{task_id}_out{ext}")
    f.save(input_path)

    try:
        remove_image_watermark(input_path, output_path, regions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try: os.remove(input_path)
        except: pass

    # 存一个简单记录方便 download 路由找到文件
    tasks[task_id] = {"output_path": output_path, "ext": ext}
    return jsonify({"task_id": task_id})


@app.route("/download/image-watermark/<task_id>")
def download_image_watermark(task_id):
    """下载去水印后的图片"""
    task = tasks.get(task_id)
    if not task or not task.get("output_path"):
        return "文件不存在", 404
    out = task["output_path"]
    if not os.path.exists(out):
        return "文件已过期", 404
    ext = task.get("ext", ".png")
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"}
    return send_file(
        out,
        as_attachment=True,
        download_name=f"nowatermark{ext}",
        mimetype=mime_map.get(ext, "application/octet-stream")
    )


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


# ═══════════════════════════════════════════════════════════════
#  视频转文字：本地上传 / 百度网盘链接 → 提取音频 → Whisper 转录
# ═══════════════════════════════════════════════════════════════

def get_whisper_model():
    """懒加载 faster-whisper base 模型（首次调用会自动下载约 150 MB）"""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def extract_audio(video_path, audio_path, log_fn):
    """用 ffmpeg 将视频音轨提取为 16 kHz 单声道 WAV"""
    log_fn("   正在提取音频轨道…")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg 失败: " + result.stderr.decode(errors="replace")[:300])
    log_fn("   音频提取完成")


def transcribe_audio(audio_path, log_fn):
    """用 faster-whisper 转录音频，返回纯文本字符串"""
    log_fn("   加载 Whisper 模型（首次运行需下载约 150 MB）…")
    model = get_whisper_model()
    log_fn("   开始语音转文字…")
    segments, _ = model.transcribe(audio_path, language="zh", beam_size=5, vad_filter=True)
    lines = [seg.text.strip() for seg in segments if seg.text.strip()]
    return "\n".join(lines)


def _do_transcribe(task_id, video_idx, video_path, title, source_line=""):
    """
    公共处理核心：（视频则提取音频）→ 转录 → 保存 TXT。
    video_path 必须是已存在的本地媒体文件路径。
    """
    task = video_tasks[task_id]
    vinfo = task["videos"][video_idx]
    tmpdir = tempfile.mkdtemp(prefix=f"aud_{task_id}_{video_idx}_")

    def log(msg):
        vinfo["logs"].append(msg)
        task["global_logs"].append(f"[{video_idx + 1}] {msg}")

    try:
        ext = Path(video_path).suffix.lower()
        if ext in AUDIO_EXTS:
            audio_path = video_path
            vinfo["status"] = "transcribing"
        else:
            vinfo["status"] = "extracting"
            audio_path = os.path.join(tmpdir, "audio.wav")
            extract_audio(video_path, audio_path, log)

        vinfo["status"] = "transcribing"
        text = transcribe_audio(audio_path, log)
        if not text.strip():
            raise RuntimeError("转录结果为空，视频可能没有语音内容")

        cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        log(f"转录完成，共 {len(text)} 字符，中文 {cn_chars} 字")

        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:80]
        txt_filename = f"{safe_title}.txt"
        txt_path = str(VIDEO_OUT_DIR / f"{task_id}_{video_idx}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            if source_line:
                f.write(f"来源：{source_line}\n")
            f.write(f"标题：{title}\n")
            f.write(f"转录时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n\n")
            f.write(text)

        vinfo["status"] = "done"
        vinfo["txt_path"] = txt_path
        vinfo["txt_filename"] = txt_filename
        log("✅ 处理完成！")

    except Exception as e:
        import traceback as tb
        vinfo["status"] = "error"
        vinfo["error"] = str(e)
        log(f"❌ 处理失败：{e}")
        log(tb.format_exc()[:500])

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        try: os.remove(video_path)
        except Exception: pass


def _baidu_download(link, extraction_code, bduss_cookie, output_dir, log_fn):
    """
    通过百度网盘下载分享文件。
    返回 (local_path, filename_without_ext)
    """
    import json as _json
    from urllib.parse import quote, urlencode

    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    cookie_str = ""
    if bduss_cookie:
        bduss_cookie = bduss_cookie.strip().replace("\n", "").replace("\r", "")
        if "=" in bduss_cookie:
            cookie_str = bduss_cookie
        else:
            cookie_str = f"BDUSS={bduss_cookie}; BDUSS_BFESS={bduss_cookie}"

    log_fn(f"   [debug] cookie长度={len(cookie_str)}")

    import requests as req
    sess = req.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Referer": "https://pan.baidu.com/",
        "Cookie": cookie_str,
    })

    m = re.search(r"/s/1([a-zA-Z0-9_-]+)", link)
    if not m:
        raise RuntimeError("链接格式无法识别，请检查是否为百度网盘分享链接")
    surl = m.group(1)

    bdstoken = ""
    if cookie_str:
        log_fn("   验证登录状态…")
        try:
            uinfo = sess.get(
                "https://pan.baidu.com/rest/2.0/xpan/nas",
                params={"method": "uinfo"}, timeout=30,
            ).json()
            log_fn(f"   [debug] uinfo={str(uinfo)[:200]}")
            if uinfo.get("errno") == 0:
                log_fn(f"   ✅ 登录成功，用户: {uinfo.get('baidu_name','')}")
            else:
                log_fn(f"   ⚠️ uinfo errno={uinfo.get('errno')}，继续尝试…")
        except Exception as e:
            log_fn(f"   [debug] uinfo异常: {e}")

        try:
            tpl = sess.get(
                "https://pan.baidu.com/api/gettemplatevariable",
                params={"clienttype": "0", "app_id": "250528",
                        "fields": _json.dumps(["bdstoken"])},
                timeout=15,
            ).json()
            if tpl.get("errno") == 0 and tpl.get("result"):
                bdstoken = tpl["result"].get("bdstoken", "")
        except Exception:
            pass
        log_fn(f"   [debug] bdstoken={'有' if bdstoken else '无'}")

    log_fn("   初始化会话…")
    sess.get(f"https://pan.baidu.com/share/init?surl={surl}", timeout=30)

    log_fn("   验证提取码…")
    verify = sess.post(
        "https://pan.baidu.com/share/verify",
        params={"surl": surl, "t": str(int(time.time() * 1000)),
                "channel": "chunlei", "web": "1", "clienttype": "0"},
        data={"pwd": extraction_code, "vcode": "", "vcode_str": ""},
        timeout=30,
    )
    vdata = verify.json()
    if vdata.get("errno") != 0:
        raise RuntimeError(
            f"提取码验证失败 (errno={vdata.get('errno')})，"
            "请检查提取码是否正确、链接是否已失效"
        )

    randsk = vdata.get("randsk", "")
    if randsk:
        sess.cookies.set("BDCLND", randsk, domain=".baidu.com")

    log_fn("   获取文件信息…")
    page = sess.get(f"https://pan.baidu.com/s/1{surl}", timeout=30).text

    filename = None
    dlink = None
    file_size = 0
    shareid = uk = fs_id = None
    sign = timestamp = None

    fl_m = re.search(r'"file_list"\s*:\s*(\[.*?\])\s*[,}]', page)
    if not fl_m:
        fl_m = re.search(r'file_list["\s:=]+(\[.*?\])', page)
    if fl_m:
        try:
            flist = _json.loads(fl_m.group(1))
            if flist:
                t = flist[0]
                filename = t.get("server_filename", "")
                dlink = t.get("dlink", "")
                file_size = int(t.get("size", 0))
                fs_id = t.get("fs_id")
        except Exception:
            pass

    for pat in [r'"shareid"\s*:\s*(\d+)', r'shareid["\s:=]+(\d+)']:
        sm = re.search(pat, page)
        if sm and sm.group(1) != "0":
            shareid = sm.group(1); break
    for pat in [r'"share_uk"\s*:\s*"?(\d+)"?', r'"uk"\s*:\s*(\d+)',
                r'share_uk["\s:=]+(\d+)']:
        um = re.search(pat, page)
        if um and um.group(1) != "0":
            uk = um.group(1); break

    sign_m = re.search(r'"sign"\s*:\s*"([^"]+)"', page)
    if sign_m: sign = sign_m.group(1)
    ts_m = re.search(r'"timestamp"\s*:\s*(\d+)', page)
    if ts_m: timestamp = ts_m.group(1)
    if not bdstoken:
        bds_m = re.search(r'"bdstoken"\s*:\s*"([a-f0-9]+)"', page)
        if bds_m: bdstoken = bds_m.group(1)

    if shareid and uk and (not filename or not dlink):
        log_fn("   通过 share/list API 获取文件详情…")
        list_params = {
            "shareid": shareid, "uk": uk, "root": "1",
            "page": "1", "num": "100",
            "channel": "chunlei", "web": "1", "clienttype": "0",
        }
        if randsk:
            list_params["sekey"] = randsk
        ldata = sess.get(
            "https://pan.baidu.com/share/list",
            params=list_params, timeout=30,
        ).json()
        log_fn(f"   [debug] share/list errno={ldata.get('errno')}")
        if ldata.get("errno") == 0 and ldata.get("list"):
            t = ldata["list"][0]
            if not filename: filename = t.get("server_filename", "")
            if not dlink: dlink = t.get("dlink", "")
            if not file_size: file_size = int(t.get("size", 0))
            if not fs_id: fs_id = t.get("fs_id")

    if not filename:
        log_fn(f"   [debug] page前500字符: {page[:500]}")
        raise RuntimeError("无法获取文件信息，请确认链接有效")

    log_fn(f"   找到文件：{filename}（{file_size // (1024*1024)} MB）")
    log_fn(f"   [debug] dlink={'有' if dlink else '无'}, sign={'有' if sign else '无'}, "
           f"ts={timestamp}, bdstoken={'有' if bdstoken else '无'}")

    if not dlink and shareid and uk and fs_id and sign and timestamp:
        log_fn("   通过 sharedownload API 获取下载链接…")
        sd = sess.post(
            "https://pan.baidu.com/api/sharedownload",
            params={"sign": sign, "timestamp": timestamp,
                    "channel": "chunlei", "web": "1", "clienttype": "0"},
            data={
                "encrypt": "0", "product": "share", "uk": uk,
                "primaryid": shareid, "fid_list": f"[{fs_id}]",
                "extra": _json.dumps({"sekey": randsk}) if randsk else "{}",
            },
            timeout=30,
        ).json()
        log_fn(f"   [debug] sharedownload errno={sd.get('errno')}")
        if sd.get("errno") == 0 and sd.get("list"):
            dlink = sd["list"][0].get("dlink", "")
    elif not dlink:
        missing = [x for x, v in [("sign", sign), ("timestamp", timestamp),
                                    ("fs_id", fs_id)] if not v]
        log_fn(f"   [debug] sharedownload 跳过，缺少: {', '.join(missing)}")

    if not dlink and cookie_str and fs_id and shareid and uk:
        log_fn("   尝试转存到网盘后获取下载链接…")
        try:
            tdata = sess.post(
                "https://pan.baidu.com/share/transfer",
                params={
                    "shareid": shareid, "from": uk,
                    "bdstoken": bdstoken, "ondup": "newcopy",
                    "channel": "chunlei", "web": "1", "clienttype": "0",
                },
                data={"fsidlist": f"[{fs_id}]", "path": "/"},
                timeout=30,
            ).json()
            t_errno = tdata.get("errno", -1)
            log_fn(f"   [debug] transfer errno={t_errno} resp={str(tdata)[:200]}")

            new_path = ""
            if t_errno == 0 and tdata.get("extra", {}).get("list"):
                new_path = tdata["extra"]["list"][0].get("to", "")
            elif t_errno in (2, -33):
                new_path = f"/{filename}"
                log_fn(f"   文件已在网盘中，直接获取下载链接…")

            if new_path:
                paths_to_try = [new_path]
                if not new_path.startswith("/来自分享/"):
                    paths_to_try.append(f"/来自分享/{filename}")

                for try_path in paths_to_try:
                    log_fn(f"   [debug] 尝试 filemetas: {try_path}")
                    mdata = sess.get(
                        "https://pan.baidu.com/api/filemetas",
                        params={
                            "target": _json.dumps([try_path]),
                            "dlink": "1", "channel": "chunlei",
                            "web": "1", "clienttype": "0", "bdstoken": bdstoken,
                        },
                        timeout=30,
                    ).json()
                    log_fn(f"   [debug] filemetas errno={mdata.get('errno')} info={str(mdata.get('info', []))[:200]}")
                    if mdata.get("errno") == 0 and mdata.get("info"):
                        dlink = mdata["info"][0].get("dlink", "")
                        if dlink:
                            break

                if not dlink:
                    log_fn("   [debug] filemetas 失败，用 list API 搜索…")
                    try:
                        search_data = sess.get(
                            "https://pan.baidu.com/rest/2.0/xpan/file",
                            params={
                                "method": "search",
                                "key": filename,
                                "recursion": "1",
                                "web": "1",
                            },
                            timeout=30,
                        ).json()
                        log_fn(f"   [debug] search errno={search_data.get('errno')} count={len(search_data.get('list', []))}")
                        if search_data.get("errno") == 0 and search_data.get("list"):
                            found = search_data["list"][0]
                            found_path = found.get("path", "")
                            log_fn(f"   [debug] 找到文件: {found_path}")
                            mdata2 = sess.get(
                                "https://pan.baidu.com/api/filemetas",
                                params={
                                    "target": _json.dumps([found_path]),
                                    "dlink": "1", "channel": "chunlei",
                                    "web": "1", "clienttype": "0", "bdstoken": bdstoken,
                                },
                                timeout=30,
                            ).json()
                            log_fn(f"   [debug] filemetas2 errno={mdata2.get('errno')}")
                            if mdata2.get("errno") == 0 and mdata2.get("info"):
                                dlink = mdata2["info"][0].get("dlink", "")
                    except Exception as se:
                        log_fn(f"   [debug] search异常: {se}")
        except Exception as te:
            log_fn(f"   [debug] 转存异常: {te}")

    if not dlink:
        raise RuntimeError(
            "无法获取下载链接。可能原因：\n"
            "1. Cookie 已过期或无效\n"
            "2. 百度账号未开通网盘或容量已满\n"
            "请重新登录 pan.baidu.com 后重新复制 Cookie"
        )

    log_fn("   开始下载…")
    dl = sess.get(
        dlink, stream=True, timeout=600,
        headers={"User-Agent": "LogStatistic"},
    )
    if dl.status_code != 200:
        raise RuntimeError(f"下载请求失败 HTTP {dl.status_code}")

    out_path = os.path.join(output_dir, filename)
    total = int(dl.headers.get("content-length", 0))
    done_bytes = 0
    last_log_pct = -10

    with open(out_path, "wb") as f:
        for chunk in dl.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            done_bytes += len(chunk)
            if total > 0:
                pct = done_bytes * 100 / total
                if pct - last_log_pct >= 10:
                    log_fn(f"   下载进度: {pct:.0f}%  "
                           f"({done_bytes // (1024*1024)}/{total // (1024*1024)} MB)")
                    last_log_pct = pct

    stem = os.path.splitext(filename)[0]
    log_fn(f"   下载完成 ({done_bytes // (1024*1024)} MB)")
    return out_path, stem


def _download_and_transcribe(task_id, video_idx, link, extraction_code, bduss_cookie):
    """下载百度网盘视频后调用公共转录核心"""
    task = video_tasks[task_id]
    vinfo = task["videos"][video_idx]
    tmpdir = tempfile.mkdtemp(prefix=f"dl_{task_id}_{video_idx}_")

    def log(msg):
        vinfo["logs"].append(msg)
        task["global_logs"].append(f"[{video_idx + 1}] {msg}")

    try:
        vinfo["status"] = "downloading"
        log("开始从百度网盘下载…")
        video_path, title = _baidu_download(
            link, extraction_code, bduss_cookie, tmpdir, log
        )
        vinfo["title"] = title
        _do_transcribe(task_id, video_idx, video_path, title, source_line=link)

    except Exception as e:
        import traceback as tb
        if vinfo["status"] != "error":
            vinfo["status"] = "error"
            vinfo["error"] = str(e)
            log(f"❌ 下载失败：{e}")
            log(tb.format_exc()[:500])

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_batch(task_id, worker_fn_args, max_workers=3):
    """通用并行批处理：worker_fn_args 为 [(fn, args), ...]"""
    task = video_tasks[task_id]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fn, *args) for fn, args in worker_fn_args]
        for f in futures:
            try: f.result()
            except Exception as e:
                task["global_logs"].append(f"未捕获异常：{e}")
    task["done"] = True
    done = sum(1 for v in task["videos"] if v["status"] == "done")
    task["global_logs"].append(f"🎉 全部完成！{done}/{len(task['videos'])} 个成功")


@app.route("/video/upload", methods=["POST"])
def video_upload():
    """接收本地视频文件（多文件），创建转录任务"""
    files = request.files.getlist("videos")
    valid = [(f, Path(f.filename).stem) for f in files
             if f.filename and Path(f.filename).suffix.lower() in VIDEO_EXTS]

    if not valid:
        return jsonify({"error": f"请选择视频文件（支持 {' / '.join(VIDEO_EXTS)}）"}), 400

    task_id = uuid.uuid4().hex
    saved = []
    for i, (f, stem) in enumerate(valid):
        ext = Path(f.filename).suffix.lower()
        save_path = str(UPLOAD_DIR / f"{task_id}_{i}{ext}")
        f.save(save_path)
        saved.append((save_path, stem))

    video_tasks[task_id] = {
        "done": False,
        "global_logs": [f"收到 {len(saved)} 个视频文件，开始并行转录…"],
        "videos": [
            {"title": stem, "status": "pending", "logs": [],
             "txt_path": None, "txt_filename": None, "error": None}
            for _, stem in saved
        ],
    }

    worker_fn_args = [
        (_do_transcribe, (task_id, i, path, stem, ""))
        for i, (path, stem) in enumerate(saved)
    ]
    threading.Thread(target=_run_batch, args=(task_id, worker_fn_args), daemon=True).start()
    return jsonify({"task_id": task_id, "count": len(saved)})


def _parse_baidu_links(raw: str):
    """解析百度网盘链接文本，支持官方分享格式和分号分隔格式"""
    links = re.findall(r'链接[：:]\s*(https?://\S+)', raw)
    codes = re.findall(r'提取码[：:]\s*([a-zA-Z0-9]+)', raw)

    if links:
        links = [re.sub(r'[，。,\s]+$', '', l) for l in links]
        if len(codes) == len(links):
            return list(zip(links, codes))
        else:
            return [(l, codes[i] if i < len(codes) else "") for i, l in enumerate(links)]

    result = []
    for item in re.split(r'[;；]+', raw):
        item = item.strip()
        if not item or not item.startswith("http"):
            continue
        parts = item.split(None, 1)
        link = parts[0]
        code = parts[1] if len(parts) > 1 else ""
        if not code:
            m = re.search(r'[?&]pwd=([a-zA-Z0-9]+)', link)
            if m:
                code = m.group(1)
        result.append((link, code))
    return result


@app.route("/video/process", methods=["POST"])
def video_process():
    """接收百度网盘链接文本，创建转录任务"""
    data = request.get_json(silent=True) or {}
    links_raw = data.get("links", "").strip()
    bduss_cookie = data.get("bduss", "").strip()

    if not links_raw:
        return jsonify({"error": "请提供至少一个百度网盘链接"}), 400

    links_data = _parse_baidu_links(links_raw)
    if not links_data:
        return jsonify({"error": "未识别到有效链接，请检查格式"}), 400

    task_id = uuid.uuid4().hex
    video_tasks[task_id] = {
        "done": False,
        "global_logs": [f"收到 {len(links_data)} 条链接，开始并行下载转录…"],
        "videos": [
            {"title": f"视频 {i + 1}", "status": "pending", "logs": [],
             "txt_path": None, "txt_filename": None, "error": None}
            for i in range(len(links_data))
        ],
    }

    worker_fn_args = [
        (_download_and_transcribe, (task_id, i, link, code, bduss_cookie))
        for i, (link, code) in enumerate(links_data)
    ]
    threading.Thread(target=_run_batch, args=(task_id, worker_fn_args), daemon=True).start()
    return jsonify({"task_id": task_id, "count": len(links_data)})


@app.route("/video/status/<task_id>")
def video_status(task_id):
    """查询批量转录任务状态"""
    task = video_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    from_idx = int(request.args.get("from", 0))
    return jsonify({
        "done": task["done"],
        "global_logs": task["global_logs"][from_idx:],
        "videos": [
            {
                "title": v["title"],
                "status": v["status"],
                "error": v["error"],
                "has_txt": v["txt_path"] is not None,
                "txt_filename": v["txt_filename"],
            }
            for v in task["videos"]
        ],
    })


@app.route("/video/download/<task_id>/<int:video_idx>")
def video_download(task_id, video_idx):
    """下载单个 TXT 转录文件"""
    task = video_tasks.get(task_id)
    if not task:
        return "任务不存在", 404
    videos = task["videos"]
    if video_idx < 0 or video_idx >= len(videos):
        return "索引无效", 404
    v = videos[video_idx]
    if not v.get("txt_path") or not os.path.exists(v["txt_path"]):
        return "文件不存在或尚未完成", 404
    return send_file(
        v["txt_path"],
        as_attachment=True,
        download_name=v["txt_filename"],
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/video/download_all/<task_id>")
def video_download_all(task_id):
    """将所有转录结果打包为 ZIP 下载"""
    task = video_tasks.get(task_id)
    if not task:
        return "任务不存在", 404

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in task["videos"]:
            if v.get("txt_path") and os.path.exists(v["txt_path"]):
                zf.write(v["txt_path"], v["txt_filename"])
                count += 1

    if count == 0:
        return "暂无可下载的转录文件", 404

    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="transcriptions.zip",
        mimetype="application/zip",
    )


# ═══════════════════════════════════════════════════════════════
#  视频加字幕：Whisper 带时间戳转录 → ASS 字幕 → ffmpeg 嵌入
# ═══════════════════════════════════════════════════════════════

def _transcribe_with_timestamps(audio_path, log_fn):
    """用 faster-whisper 转录音频，返回 [(start, end, text), ...] 时间戳列表"""
    log_fn("加载 Whisper 模型…")
    model = get_whisper_model()
    log_fn("开始语音识别（带时间戳）…")
    segments, _ = model.transcribe(
        audio_path, language="zh", beam_size=5, vad_filter=True,
    )
    result = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            result.append((seg.start, seg.end, text))
    log_fn(f"识别完成，共 {len(result)} 条字幕")
    return result


def _format_ass_time(seconds):
    """将秒数转为 ASS 时间格式 H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _generate_ass(segments, font_size, video_width=1920, video_height=1080):
    """根据时间戳列表生成 ASS 字幕文件内容"""
    header = f"""[Script Info]
Title: Auto Generated Subtitles
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,PingFang SC,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,20,20,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for start, end, text in segments:
        s = _format_ass_time(start)
        e = _format_ass_time(end)
        lines.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{text}")

    return header + "\n".join(lines) + "\n"


def _get_video_resolution(video_path):
    """用 ffprobe 获取视频分辨率"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            video_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        out = r.stdout.decode().strip()
        if "x" in out:
            w, h = out.split("x")
            return int(w), int(h)
    except Exception:
        pass
    return 1920, 1080


def _embed_subtitles(video_path, ass_path, output_path, log_fn):
    """将 ASS 字幕硬烧录进视频画面（任意格式均支持）"""
    log_fn("正在将字幕烧录进画面…")
    import shutil as _shutil, tempfile as _tempfile
    # 复制到无特殊字符的路径，以相对路径传给 ffmpeg ass filter
    work_dir = _tempfile.mkdtemp(prefix="ffass_")
    _shutil.copy2(ass_path, os.path.join(work_dir, "s.ass"))
    # 优先使用带 libass 的 ffmpeg-full
    ffmpeg_bin = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    if not os.path.exists(ffmpeg_bin):
        ffmpeg_bin = "ffmpeg"
    try:
        cmd = [
            ffmpeg_bin, "-y",
            "-i", os.path.abspath(video_path),
            "-vf", "ass=s.ass",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "copy",
            os.path.abspath(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=1800, cwd=work_dir)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[-800:]
            raise RuntimeError(f"ffmpeg 字幕嵌入失败:\n{stderr}")
    finally:
        _shutil.rmtree(work_dir, ignore_errors=True)
    log_fn("字幕视频生成完成")


def _do_subtitle(task_id, video_path, title, font_size_name):
    """字幕处理核心：提取音频 → 转录 → 生成 ASS → 嵌入字幕"""
    task = subtitle_tasks[task_id]
    tmpdir = tempfile.mkdtemp(prefix=f"sub_{task_id}_")

    def log(msg):
        task["logs"].append(msg)

    try:
        font_size = FONT_SIZES.get(font_size_name, 24)

        audio_path = os.path.join(tmpdir, "audio.wav")
        extract_audio(video_path, audio_path, log)

        segments = _transcribe_with_timestamps(audio_path, log)

        video_w, video_h = _get_video_resolution(video_path)
        ass_content = _generate_ass(segments, font_size, video_w, video_h)
        ass_path = os.path.join(tmpdir, "subtitles.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # 硬字幕：输出保持原格式（MP4/MOV 等均支持）
        ext = os.path.splitext(video_path)[1] or ".mp4"
        output_name = f"{title}_字幕{ext}"
        output_path = str(SUBTITLE_OUT_DIR / f"{task_id}{ext}")
        _embed_subtitles(video_path, ass_path, output_path, log)

        task["output_path"] = output_path
        task["output_name"] = output_name
        task["success"] = True
        log(f"全部完成！可下载: {output_name}")

    except Exception as e:
        import traceback as tb
        task["success"] = False
        task["error"] = str(e)
        log(f"❌ 处理失败：{e}")
        log(tb.format_exc()[:500])

    finally:
        task["done"] = True
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            os.remove(video_path)
        except Exception:
            pass


@app.route("/subtitle/upload", methods=["POST"])
def subtitle_upload():
    """接收视频文件和字体大小，创建字幕任务"""
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify({"error": "请选择视频文件"}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in VIDEO_EXTS:
        return jsonify({"error": f"不支持的格式 {ext}"}), 400

    font_size = request.form.get("font_size", "medium")
    if font_size not in FONT_SIZES:
        font_size = "medium"

    task_id = uuid.uuid4().hex
    save_path = str(UPLOAD_DIR / f"{task_id}{ext}")
    f.save(save_path)
    title = Path(f.filename).stem

    subtitle_tasks[task_id] = {
        "done": False,
        "success": False,
        "logs": [f"收到视频: {f.filename}，字体大小: {font_size}"],
        "output_path": None,
        "output_name": None,
        "error": None,
    }

    threading.Thread(
        target=_do_subtitle,
        args=(task_id, save_path, title, font_size),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/subtitle/status/<task_id>")
def subtitle_status(task_id):
    """查询字幕任务状态"""
    task = subtitle_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    from_idx = int(request.args.get("from", 0))
    return jsonify({
        "done": task["done"],
        "success": task["success"],
        "logs": task["logs"][from_idx:],
        "output_name": task["output_name"],
        "error": task["error"],
    })


@app.route("/subtitle/download/<task_id>")
def subtitle_download(task_id):
    """下载带字幕的视频"""
    task = subtitle_tasks.get(task_id)
    if not task:
        return "任务不存在", 404
    if not task.get("output_path") or not os.path.exists(task["output_path"]):
        return "文件不存在或尚未完成", 404
    return send_file(
        task["output_path"],
        as_attachment=True,
        download_name=task["output_name"],
    )



    print("🚀 启动服务：http://127.0.0.1:5000")
    # 启动 Flask 应用，监听所有网络接口
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, host="0.0.0.0", port=port)
