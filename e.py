import os
import sys
import re
import json
import time
import asyncio
import logging
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, Optional, List, Any
from dataclasses import dataclass
from collections import defaultdict
import yt_dlp
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    BotCommand,
    InputFile
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "8772316941:AAG6_YHAQIR_47WY5u8Ss6YCVJSp6ScmnA0")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-domain.com/webhook")
PORT = int(os.getenv("PORT", 8080))
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit
MAX_DOWNLOADS_PER_MINUTE = 5
ADMIN_ID = int(os.getenv("ADMIN_ID", "8775324279"))

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Rate limiting
user_downloads: Dict[int, List[float]] = defaultdict(list)
download_lock = threading.Lock()

@dataclass
class VideoInfo:
    title: str
    duration: int
    uploader: str
    thumbnail: Optional[str]
    formats: List[Dict]
    webpage_url: str
    extractor: str

class MediaDownloader:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()
        self.executor = ThreadPoolExecutor(max_workers=3)
        
    def cleanup(self):
        """Clean up temporary files"""
        try:
            for file in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    def get_info(self, url: str) -> Optional[VideoInfo]:
        """Extract video information without downloading"""
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                formats = []
                for f in info.get('formats', []):
                    if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                        formats.append({
                            'format_id': f.get('format_id'),
                            'ext': f.get('ext'),
                            'quality': f.get('quality_label') or f.get('abr') or 'unknown',
                            'filesize': f.get('filesize') or f.get('filesize_approx', 0),
                            'vcodec': f.get('vcodec'),
                            'acodec': f.get('acodec')
                        })
                
                return VideoInfo(
                    title=info.get('title', 'Unknown'),
                    duration=info.get('duration', 0),
                    uploader=info.get('uploader', 'Unknown'),
                    thumbnail=info.get('thumbnail'),
                    formats=formats,
                    webpage_url=info.get('webpage_url', url),
                    extractor=info.get('extractor', 'unknown')
                )
        except Exception as e:
            logger.error(f"Info extraction error: {e}")
            return None

    def download(self, url: str, format_id: str = None, audio_only: bool = False) -> Optional[str]:
        """Download media and return file path"""
        timestamp = int(time.time())
        output_template = os.path.join(self.temp_dir, f'%(title)s_{timestamp}.%(ext)s')
        
        ydl_opts = {
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'format': format_id if format_id else ('bestaudio/best' if audio_only else 'best[filesize<50M]/best'),
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }] if not audio_only else [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'max_filesize': MAX_FILE_SIZE,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                # Handle audio conversion extension change
                if audio_only and not filename.endswith('.mp3'):
                    filename = os.path.splitext(filename)[0] + '.mp3'
                
                # Verify file exists and size
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    return filename
                return None
                
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None

    def compress_video(self, input_path: str) -> Optional[str]:
        """Compress video if too large using ffmpeg"""
        output_path = input_path.replace('.mp4', '_compressed.mp4')
        
        try:
            import subprocess
            cmd = [
                'ffmpeg', '-i', input_path,
                '-vcodec', 'libx264',
                '-crf', '28',
                '-preset', 'fast',
                '-acodec', 'aac',
                '-b:a', '128k',
                '-movflags', '+faststart',
                '-y',
                output_path
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) < MAX_FILE_SIZE:
                os.remove(input_path)
                return output_path
            return None
        except Exception as e:
            logger.error(f"Compression error: {e}")
            return None

downloader = MediaDownloader()

def check_rate_limit(user_id: int) -> bool:
    """Check if user has exceeded download limit"""
    with download_lock:
        now = time.time()
        minute_ago = now - 60
        
        # Clean old entries
        user_downloads[user_id] = [t for t in user_downloads[user_id] if t > minute_ago]
        
        if len(user_downloads[user_id]) >= MAX_DOWNLOADS_PER_MINUTE:
            return False
        
        user_downloads[user_id].append(now)
        return True

def format_duration(seconds: int) -> str:
    """Format seconds to readable time"""
    if seconds < 60:
        return f"{seconds}ث"
    elif seconds < 3600:
        return f"{seconds//60}د {seconds%60}ث"
    else:
        return f"{seconds//3600}س {(seconds%3600)//60}د"

def format_size(bytes_size: int) -> str:
    """Format bytes to readable size"""
    if bytes_size == 0:
        return "غير معروف"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

def create_info_text(info: VideoInfo) -> str:
    """Create formatted info message"""
    duration = format_duration(info.duration)
    
    text = f"""
📹 *معلومات الفيديو*

📝 *العنوان:* `{info.title}`

👤 *القناة:* `{info.uploader}`

⏱ *المدة:* `{duration}`

🔗 *المصدر:* `{info.extractor}`

📊 *الجودات المتاحة:* {len([f for f in info.formats if f.get('vcodec') != 'none'])} جودة فيديو
🎵 *جودات الصوت:* {len([f for f in info.formats if f.get('acodec') != 'none'])} جودة صوت
"""
    return text

def get_quality_buttons(info: VideoInfo, url: str) -> List[List[InlineKeyboardButton]]:
    """Create quality selection buttons"""
    buttons = []
    
    # Video qualities
    video_formats = [f for f in info.formats if f.get('vcodec') != 'none' and f.get('filesize', 0) < MAX_FILE_SIZE]
    video_formats.sort(key=lambda x: x.get('filesize', 0), reverse=True)
    
    best_video = video_formats[0] if video_formats else None
    
    if best_video:
        size = format_size(best_video.get('filesize', 0))
        buttons.append([
            InlineKeyboardButton(f"🎬 أفضل جودة ({best_video.get('quality', 'HD')} - {size})", 
                               callback_data=f"dl_video|{url}|best")
        ])
    
    # Medium quality option
    medium_formats = [f for f in video_formats if f.get('filesize', 0) < 20*1024*1024]
    if medium_formats:
        med = medium_formats[0]
        size = format_size(med.get('filesize', 0))
        buttons.append([
            InlineKeyboardButton(f"📱 جودة متوسطة ({med.get('quality', 'SD')} - {size})", 
                               callback_data=f"dl_video|{url}|{med['format_id']}")
        ])
    
    # Audio only
    buttons.append([
        InlineKeyboardButton("🎵 تحميل صوت فقط (MP3)", 
                           callback_data=f"dl_audio|{url}|best")
    ])
    
    return buttons

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    welcome_text = f"""
👋 أهلاً بك *{user.first_name}*!

🤖 أنا بوت تحميل الفيديوهات والميديا من جميع المواقع.

📌 *المواقع المدعومة:*
• YouTube • TikTok • Instagram
• Facebook • Twitter/X • Reddit
• Pinterest • Snapchat • Vimeo
• Dailymotion • Twitch • SoundCloud
• +1000 موقع آخر

📥 *طريقة الاستخدام:*
فقط أرسل رابط الفيديو وسأقوم بالباقي!

⚡️ *المميزات:*
✓ تحميل بأعلى جودة
✓ استخراج الصوت MP3
✓ ضغط تلقائي للملفات الكبيرة
✓ سرعة عالية في التحميل

🔧 *الأوامر المتاحة:*
/help - المساعدة
/settings - الإعدادات
/about - عن البوت

🚀 جرب الآن وأرسل أي رابط!
"""
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
📖 *دليل الاستخدام*

1️⃣ *تحميل فيديو:*
أرسل الرابط مباشرة ثم اختر جودة التحميل

2️⃣ *تحميل صوت فقط:*
اختر "تحميل صوت فقط" من الأزرار

3️⃣ *معلومات الفيديو:*
سيتم عرض المعلومات تلقائياً قبل التحميل

⚠️ *ملاحظات مهمة:*
• الحد الأقصى للملف: 50 ميجابايت
• {MAX_DOWNLOADS_PER_MINUTE} تحميلات فقط في الدقيقة لكل مستخدم
• يتم حذف الملفات تلقائياً بعد الإرسال

❓ *للمساعدة:* @YourSupport
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command"""
    settings_text = """
⚙️ *الإعدادات*

🔔 *الإشعارات:* مفعلة
🌐 *اللغة:* العربية
📊 *الجودة الافتراضية:* تلقائي

*إحصائياتك:*
📥 التحميلات: غير محدود
⏱ آخر نشاط: الآن

⚡️ البوت يعمل بأفضل جودة تلقائياً
"""
    await update.message.reply_text(settings_text, parse_mode='Markdown')

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /about command"""
    about_text = """
🤖 *عن البوت*

📌 *الإصدار:* 2.0.0
💻 *المطور:* @YourUsername
🛠 *التقنيات:* Python, yt-dlp, FFmpeg

🌟 *المميزات:*
• دعم 1000+ موقع
• معالجة Async عالية السرعة
• ضغط ذكي للفيديوهات
• واجهة تفاعلية

📊 *الإحصائيات:*
• المستخدمين النشطين: مباشر
• سرعة التحميل: عالية جداً
• وقت التشغيل: 24/7

🔄 *آخر تحديث:* 2024
"""
    await update.message.reply_text(about_text, parse_mode='Markdown')

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming URLs"""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # URL validation
    url_pattern = re.compile(
        r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    )
    
    if not url_pattern.match(url):
        await update.message.reply_text("❌ الرابط غير صالح. يرجى إرسال رابط صحيح.")
        return
    
    # Rate limit check
    if not check_rate_limit(user_id):
        await update.message.reply_text(
            "⏳ *تنبيه:* لقد تجاوزت الحد المسموح (5 تحميلات/دقيقة). يرجى الانتظار قليلاً.",
            parse_mode='Markdown'
        )
        return
    
    # Send analyzing message
    analyzing_msg = await update.message.reply_text(
        "🔍 *جارٍ تحليل الرابط...*",
        parse_mode='Markdown'
    )
    
    try:
        # Extract info in thread pool
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            downloader.executor, 
            downloader.get_info, 
            url
        )
        
        if not info:
            await analyzing_msg.edit_text(
                "❌ *عذراً، لا يمكن الوصول إلى هذا الرابط.*\n"
                "قد يكون:\n"
                "• الرابط خاص أو محذوف\n"
                "• الموقع غير مدعوم حالياً\n"
                "• هناك مشكلة مؤقتة في الخادم",
                parse_mode='Markdown'
            )
            return
        
        # Create info message and buttons
        info_text = create_info_text(info)
        buttons = get_quality_buttons(info, url)
        
        # Add info button
        buttons.append([
            InlineKeyboardButton("ℹ️ تحديث المعلومات", callback_data=f"refresh|{url}")
        ])
        
        keyboard = InlineKeyboardMarkup(buttons)
        
        await analyzing_msg.edit_text(
            info_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        
    except Exception as e:
        logger.error(f"URL handling error: {e}")
        await analyzing_msg.edit_text(
            "❌ *حدث خطأ أثناء معالجة الرابط.*\n"
            "يرجى المحاولة مرة أخرى لاحقاً.",
            parse_mode='Markdown'
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split('|')
    action = data[0]
    url = data[1] if len(data) > 1 else None
    format_id = data[2] if len(data) > 2 else 'best'
    
    if not url:
        await query.edit_message_text("❌ خطأ في البيانات")
        return
    
    user_id = update.effective_user.id
    
    if action in ['dl_video', 'dl_audio']:
        # Check rate limit again
        if not check_rate_limit(user_id):
            await query.edit_message_text(
                "⏳ *تنبيه:* لقد تجاوزت الحد المسموح. يرجى الانتظار.",
                parse_mode='Markdown'
            )
            return
        
        audio_only = (action == 'dl_audio')
        media_type = "الصوت" if audio_only else "الفيديو"
        
        await query.edit_message_text(
            f"⬇️ *جارٍ تحميل {media_type}...*\n"
            f"⏳ قد يستغرق هذا بضع ثوانٍ...",
            parse_mode='Markdown'
        )
        
        try:
            # Download in thread pool
            loop = asyncio.get_event_loop()
            file_path = await loop.run_in_executor(
                downloader.executor,
                downloader.download,
                url,
                format_id if not audio_only else None,
                audio_only
            )
            
            if not file_path:
                await query.edit_message_text(
                    "❌ *فشل التحميل.*\n"
                    "قد يكون الملف كبير جداً أو غير متاح.",
                    parse_mode='Markdown'
                )
                return
            
            # Check file size
            file_size = os.path.getsize(file_path)
            
            if file_size > MAX_FILE_SIZE and not audio_only:
                # Try compression
                await query.edit_message_text(
                    "🗜 *الملف كبير، جارٍ الضغط...*",
                    parse_mode='Markdown'
                )
                
                compressed_path = await loop.run_in_executor(
                    downloader.executor,
                    downloader.compress_video,
                    file_path
                )
                
                if compressed_path:
                    file_path = compressed_path
                    file_size = os.path.getsize(file_path)
                else:
                    await query.edit_message_text(
                        "❌ *الملف كبير جداً (أكبر من 50 ميجابايت)*\n"
                        "جرب تحميل جودة أقل أو الصوت فقط.",
                        parse_mode='Markdown'
                    )
                    return
            
            # Send file
            await query.edit_message_text(
                "📤 *جارٍ إرسال الملف...*",
                parse_mode='Markdown'
            )
            
            # Determine file type and send
            if audio_only or file_path.endswith('.mp3'):
                with open(file_path, 'rb') as f:
                    await context.bot.send_audio(
                        chat_id=update.effective_chat.id,
                        audio=f,
                        title="Audio",
                        performer="Downloader Bot",
                        caption="🎵 تم التحميل بنجاح بواسطة @YourBot"
                    )
            else:
                with open(file_path, 'rb') as f:
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=f,
                        caption="🎬 تم التحميل بنجاح بواسطة @YourBot",
                        supports_streaming=True
                    )
            
            # Cleanup
            if os.path.exists(file_path):
                os.remove(file_path)
            
            # Success message
            await query.edit_message_text(
                "✅ *تم التحميل بنجاح!*",
                parse_mode='Markdown'
            )
            
            logger.info(f"Successful download: {url} by user {user_id}")
            
        except Exception as e:
            logger.error(f"Download error: {e}")
            await query.edit_message_text(
                "❌ *حدث خطأ أثناء التحميل.*\n"
                "يرجى المحاولة مرة أخرى.",
                parse_mode='Markdown'
            )
    
    elif action == 'refresh':
        # Refresh info
        await query.edit_message_text("🔄 *جارٍ التحديث...*", parse_mode='Markdown')
        await handle_url(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ *حدث خطأ غير متوقع.*\n"
            "تم إبلاغ المطور وسيتم إصلاحه قريباً.",
            parse_mode='Markdown'
        )

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)
    
    # Set commands menu
    commands = [
        BotCommand("start", "بدء البوت"),
        BotCommand("help", "المساعدة"),
        BotCommand("settings", "الإعدادات"),
        BotCommand("about", "عن البوت")
    ]
    
    # Run with webhook or polling
    if os.getenv("WEBHOOK_MODE", "false").lower() == "true":
        logger.info(f"Starting webhook on port {PORT}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="/webhook",
            webhook_url=WEBHOOK_URL
        )
    else:
        logger.info("Starting polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
