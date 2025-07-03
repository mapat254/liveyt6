import streamlit as st
import pandas as pd
import psutil
import subprocess
import os
import json
import time
from datetime import datetime, timedelta
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import requests

# YouTube API scopes
SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']

# Jakarta timezone
JAKARTA_TZ = pytz.timezone('Asia/Jakarta')

# Thumbnail upload tracking
THUMBNAIL_TRACKING_FILE = 'thumbnail_uploads.json'

def load_thumbnail_tracking():
    """Load thumbnail upload tracking data"""
    try:
        if os.path.exists(THUMBNAIL_TRACKING_FILE):
            with open(THUMBNAIL_TRACKING_FILE, 'r') as f:
                data = json.load(f)
                # Clean old entries (older than 24 hours)
                current_time = time.time()
                data['uploads'] = [upload for upload in data['uploads'] 
                                 if current_time - upload['timestamp'] < 86400]
                return data
        return {'uploads': []}
    except:
        return {'uploads': []}

def save_thumbnail_tracking(data):
    """Save thumbnail upload tracking data"""
    try:
        with open(THUMBNAIL_TRACKING_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def can_upload_thumbnail():
    """Check if thumbnail upload is allowed based on rate limits"""
    data = load_thumbnail_tracking()
    current_time = time.time()
    
    # Count uploads in last 24 hours
    daily_uploads = len([upload for upload in data['uploads'] 
                        if current_time - upload['timestamp'] < 86400])
    
    # Count uploads in last hour
    hourly_uploads = len([upload for upload in data['uploads'] 
                         if current_time - upload['timestamp'] < 3600])
    
    # Conservative limits: 50/day, 10/hour
    return daily_uploads < 50 and hourly_uploads < 10, daily_uploads, hourly_uploads

def record_thumbnail_upload():
    """Record a thumbnail upload"""
    data = load_thumbnail_tracking()
    data['uploads'].append({'timestamp': time.time()})
    save_thumbnail_tracking(data)

def get_jakarta_time():
    """Get current Jakarta time"""
    return datetime.now(JAKARTA_TZ)

def authenticate_youtube():
    """Authenticate with YouTube API"""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.path.exists('credentials.json'):
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            else:
                st.error("❌ File credentials.json tidak ditemukan!")
                st.info("📋 Upload file credentials.json dari Google Cloud Console")
                return None
        
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    
    return build('youtube', 'v3', credentials=creds)

def upload_thumbnail(service, video_id, thumbnail_path):
    """Upload thumbnail with rate limiting"""
    can_upload, daily_count, hourly_count = can_upload_thumbnail()
    
    if not can_upload:
        return False, f"Rate limit exceeded. Daily: {daily_count}/50, Hourly: {hourly_count}/10"
    
    try:
        media = MediaFileUpload(thumbnail_path, mimetype='image/jpeg', resumable=True)
        request = service.thumbnails().set(
            videoId=video_id,
            media_body=media
        )
        response = request.execute()
        record_thumbnail_upload()
        return True, "Thumbnail uploaded successfully"
    except HttpError as e:
        error_msg = str(e)
        if "429" in error_msg or "uploadRateLimitExceeded" in error_msg:
            return False, "YouTube rate limit exceeded. Please try again later (max 10/hour, 50/day)."
        elif "403" in error_msg:
            return False, "Permission denied. Make sure your API has thumbnail upload permissions."
        else:
            return False, f"Error uploading thumbnail: {e}"

def create_youtube_broadcast(service, title, description, start_time, privacy_status, quality, thumbnail_path=None):
    """Create YouTube live broadcast with enhanced error handling"""
    try:
        # Create broadcast
        broadcast_response = service.liveBroadcasts().insert(
            part='snippet,status,contentDetails',
            body={
                'snippet': {
                    'title': title,
                    'description': description,
                    'scheduledStartTime': start_time.isoformat(),
                },
                'status': {
                    'privacyStatus': privacy_status,
                    'selfDeclaredMadeForKids': False
                },
                'contentDetails': {
                    'enableAutoStart': True,
                    'enableAutoStop': True,
                    'recordFromStart': True,
                    'enableDvr': True,
                    'enableContentEncryption': False,
                    'enableEmbed': True,
                    'monitorStream': {
                        'enableMonitorStream': False,
                        'broadcastStreamDelayMs': 0
                    }
                }
            }
        ).execute()
        
        broadcast_id = broadcast_response['id']
        
        # Enhanced stream creation with proper resolution mapping
        quality_mapping = {
            '720p': {'resolution': '720p', 'format': '720p'},
            '1080p': {'resolution': '1080p', 'format': '1080p'},
            '480p': {'resolution': '480p', 'format': '480p'},
            '360p': {'resolution': '360p', 'format': '360p'}
        }
        
        stream_quality = quality_mapping.get(quality, quality_mapping['720p'])
        
        # Create stream with enhanced configuration
        stream_response = service.liveStreams().insert(
            part='snippet,cdn',
            body={
                'snippet': {
                    'title': f'{title} - Stream',
                    'description': f'Live stream for: {title}'
                },
                'cdn': {
                    'format': stream_quality['format'],
                    'ingestionType': 'rtmp',
                    'resolution': stream_quality['resolution'],
                    'frameRate': '30fps'
                }
            }
        ).execute()
        
        stream_id = stream_response['id']
        stream_key = stream_response['cdn']['ingestionInfo']['streamName']
        rtmp_url = stream_response['cdn']['ingestionInfo']['ingestionAddress']
        
        # Bind broadcast to stream
        service.liveBroadcasts().bind(
            part='id,contentDetails',
            id=broadcast_id,
            streamId=stream_id
        ).execute()
        
        # Upload thumbnail if provided
        thumbnail_success = True
        thumbnail_message = ""
        if thumbnail_path and os.path.exists(thumbnail_path):
            thumbnail_success, thumbnail_message = upload_thumbnail(service, broadcast_id, thumbnail_path)
            if not thumbnail_success:
                st.warning(f"⚠️ Broadcast created but thumbnail upload failed: {thumbnail_message}")
        
        return {
            'broadcast_id': broadcast_id,
            'stream_id': stream_id,
            'stream_key': stream_key,
            'rtmp_url': rtmp_url,
            'youtube_url': f'https://www.youtube.com/watch?v={broadcast_id}',
            'thumbnail_uploaded': thumbnail_success,
            'title': title,
            'quality': quality,
            'start_time': start_time.isoformat(),
            'privacy_status': privacy_status
        }
        
    except HttpError as e:
        error_msg = str(e)
        if "Resolution is required" in error_msg:
            return None, "Stream resolution error. Please try again with a different quality setting."
        elif "429" in error_msg:
            return None, "YouTube API rate limit exceeded. Please wait a few minutes and try again."
        elif "madeForKids" in error_msg:
            return None, "Error: Use selfDeclaredMadeForKids instead of madeForKids field."
        else:
            return None, f"Error creating broadcast: {e}"

def get_system_info():
    """Get system information"""
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    return {
        'cpu_percent': cpu_percent,
        'memory_percent': memory.percent,
        'memory_available': memory.available // (1024**3),  # GB
        'disk_percent': disk.percent,
        'disk_free': disk.free // (1024**3)  # GB
    }

def start_streaming_with_recovery(rtmp_url, stream_key, video_source, quality, duration_minutes=None):
    """Start streaming with enhanced error recovery and reconnection"""
    
    # Quality settings with optimized parameters for stability
    quality_settings = {
        '360p': {
            'video_bitrate': '800k',
            'audio_bitrate': '96k',
            'resolution': '640x360',
            'fps': '25',  # Reduced FPS for stability
            'preset': 'veryfast',
            'bufsize': '1600k'
        },
        '480p': {
            'video_bitrate': '1200k',
            'audio_bitrate': '128k',
            'resolution': '854x480',
            'fps': '25',
            'preset': 'veryfast',
            'bufsize': '2400k'
        },
        '720p': {
            'video_bitrate': '2000k',
            'audio_bitrate': '128k',
            'resolution': '1280x720',
            'fps': '25',
            'preset': 'fast',
            'bufsize': '4000k'
        },
        '1080p': {
            'video_bitrate': '4000k',
            'audio_bitrate': '192k',
            'resolution': '1920x1080',
            'fps': '25',
            'preset': 'medium',
            'bufsize': '8000k'
        }
    }
    
    settings = quality_settings.get(quality, quality_settings['720p'])
    full_rtmp_url = f"{rtmp_url}/{stream_key}"
    
    # Enhanced FFmpeg command with recovery options
    base_cmd = [
        'ffmpeg',
        '-re',  # Read input at native frame rate
        '-i', video_source,
        '-c:v', 'libx264',
        '-preset', settings['preset'],
        '-b:v', settings['video_bitrate'],
        '-maxrate', settings['video_bitrate'],
        '-bufsize', settings['bufsize'],
        '-vf', f"scale={settings['resolution']}",
        '-r', settings['fps'],
        '-g', '50',  # GOP size
        '-keyint_min', '25',
        '-sc_threshold', '0',
        '-c:a', 'aac',
        '-b:a', settings['audio_bitrate'],
        '-ar', '44100',
        '-ac', '2',
        '-f', 'flv',
        
        # Enhanced RTMP options for stability
        '-rtmp_live', 'live',
        '-rtmp_buffer', '1000',
        '-rtmp_conn', 'S:publish',
        '-rtmp_flashver', 'FMLE/3.0',
        '-rtmp_pageurl', 'https://www.youtube.com',
        '-rtmp_swfurl', 'https://www.youtube.com',
        
        # Network resilience options
        '-timeout', '10000000',  # 10 second timeout
        '-reconnect', '1',
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        
        # Error handling
        '-avoid_negative_ts', 'make_zero',
        '-fflags', '+genpts',
        '-use_wallclock_as_timestamps', '1',
        
        full_rtmp_url
    ]
    
    # Add duration if specified
    if duration_minutes:
        base_cmd.insert(-1, '-t')
        base_cmd.insert(-1, str(duration_minutes * 60))
    
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            st.info(f"🚀 Starting stream (Attempt {retry_count + 1}/{max_retries})")
            st.info(f"📺 Quality: {quality} | Bitrate: {settings['video_bitrate']} | Resolution: {settings['resolution']}")
            
            # Start streaming process
            process = subprocess.Popen(
                base_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )
            
            # Store process info
            st.session_state.streaming_process = process
            st.session_state.streaming_active = True
            
            # Monitor streaming with enhanced error detection
            error_patterns = [
                'Broken pipe',
                'Connection refused',
                'Network is unreachable',
                'Operation timed out',
                'Server returned 4XX',
                'Server returned 5XX',
                'rtmp_write_packet',
                'Failed to update header'
            ]
            
            consecutive_errors = 0
            last_error_time = 0
            
            while process.poll() is None:
                line = process.stderr.readline()
                if line:
                    # Check for critical errors
                    if any(pattern in line for pattern in error_patterns):
                        consecutive_errors += 1
                        current_time = time.time()
                        
                        if consecutive_errors >= 3 or (current_time - last_error_time) < 10:
                            st.error(f"❌ Critical streaming error detected: {line.strip()}")
                            process.terminate()
                            break
                        
                        last_error_time = current_time
                    else:
                        consecutive_errors = 0
                    
                    # Show progress for successful streaming
                    if 'frame=' in line and 'fps=' in line:
                        st.text(f"📊 {line.strip()}")
                
                time.sleep(0.1)
            
            # Check exit status
            return_code = process.wait()
            
            if return_code == 0:
                st.success("✅ Streaming completed successfully!")
                break
            else:
                st.error(f"❌ Streaming failed with code: {return_code}")
                
                # Get error output
                _, stderr = process.communicate()
                if stderr:
                    st.error(f"Error details: {stderr}")
                
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = min(30, 5 * retry_count)  # Progressive backoff
                    st.warning(f"⏳ Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    st.error("❌ Max retries reached. Streaming failed.")
                    
        except Exception as e:
            st.error(f"❌ Streaming error: {e}")
            retry_count += 1
            if retry_count < max_retries:
                st.warning(f"⏳ Retrying in 10 seconds...")
                time.sleep(10)
        finally:
            st.session_state.streaming_active = False
            if 'streaming_process' in st.session_state:
                try:
                    st.session_state.streaming_process.terminate()
                except:
                    pass

def stop_streaming():
    """Stop active streaming"""
    if 'streaming_process' in st.session_state and st.session_state.streaming_process:
        try:
            st.session_state.streaming_process.terminate()
            st.session_state.streaming_process.wait(timeout=5)
            st.success("✅ Streaming stopped successfully")
        except subprocess.TimeoutExpired:
            st.session_state.streaming_process.kill()
            st.warning("⚠️ Streaming process forcefully terminated")
        except Exception as e:
            st.error(f"❌ Error stopping stream: {e}")
        finally:
            st.session_state.streaming_active = False
            st.session_state.streaming_process = None

def get_video_files():
    """Get list of video files in current directory"""
    video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v']
    video_files = []
    
    try:
        for file in os.listdir('.'):
            if any(file.lower().endswith(ext) for ext in video_extensions):
                file_size = os.path.getsize(file) / (1024**2)  # MB
                video_files.append({
                    'name': file,
                    'size': f"{file_size:.1f} MB",
                    'path': file
                })
    except Exception as e:
        st.error(f"Error reading video files: {e}")
    
    return video_files

def load_streams():
    """Load saved streams from file"""
    try:
        if os.path.exists('streams.json'):
            with open('streams.json', 'r') as f:
                return json.load(f)
        return []
    except:
        return []

def save_streams(streams):
    """Save streams to file"""
    try:
        with open('streams.json', 'w') as f:
            json.dump(streams, f, indent=2)
    except Exception as e:
        st.error(f"Error saving streams: {e}")

def main():
    st.set_page_config(
        page_title="🎥 YouTube Live Stream Manager",
        page_icon="🎥",
        layout="wide"
    )
    
    st.title("🎥 YouTube Live Stream Manager")
    
    # Initialize session state
    if 'streaming_active' not in st.session_state:
        st.session_state.streaming_active = False
    if 'streaming_process' not in st.session_state:
        st.session_state.streaming_process = None
    if 'active_tab' not in st.session_state:
        st.session_state.active_tab = 0
    if 'new_stream_data' not in st.session_state:
        st.session_state.new_stream_data = None
    
    # Sidebar
    with st.sidebar:
        st.header("📊 System Status")
        
        # System info
        sys_info = get_system_info()
        st.metric("💻 CPU Usage", f"{sys_info['cpu_percent']:.1f}%")
        st.metric("🧠 Memory Usage", f"{sys_info['memory_percent']:.1f}%")
        st.metric("💾 Disk Usage", f"{sys_info['disk_percent']:.1f}%")
        
        st.divider()
        
        # Jakarta time
        jakarta_time = get_jakarta_time()
        st.header("🕐 Waktu Jakarta")
        st.write(f"**Waktu Sekarang:** {jakarta_time.strftime('%H:%M:%S WIB')}")
        st.write(f"**Tanggal:** {jakarta_time.strftime('%d %B %Y')}")
        
        st.divider()
        
        # Thumbnail quota status
        can_upload, daily_count, hourly_count = can_upload_thumbnail()
        st.header("📸 Thumbnail Upload Status")
        st.write(f"**Today:** {daily_count}/50 uploads")
        st.write(f"**Last Hour:** {hourly_count}/10 uploads")
        
        if can_upload:
            st.success("✅ Upload quota available")
        else:
            st.error("❌ Upload quota exceeded")
        
        st.divider()
        
        # Streaming status
        st.header("🎬 Streaming Status")
        if st.session_state.streaming_active:
            st.error("🔴 STREAMING ACTIVE")
            if st.button("⏹️ Stop Streaming", type="primary"):
                stop_streaming()
        else:
            st.success("⚪ Ready to Stream")
    
    # Main tabs with dynamic selection
    tab_names = ["🎬 Create Broadcast", "➕ Add New Stream", "📺 Manage Streams", "🎥 Start Streaming"]
    
    # Create tabs
    tabs = st.tabs(tab_names)
    
    # Tab 1: Create Broadcast
    with tabs[0]:
        st.header("🎬 Create YouTube Live Broadcast")
        
        # Authenticate
        service = authenticate_youtube()
        if not service:
            st.stop()
        
        col1, col2 = st.columns(2)
        
        with col1:
            title = st.text_input("📝 Broadcast Title", value="Live Stream")
            description = st.text_area("📄 Description", value="Live streaming session")
            
            # Time selection with Jakarta timezone
            current_jakarta = get_jakarta_time()
            
            time_option = st.radio(
                "⏰ Start Time",
                ["🚀 Start Now", "⏰ Schedule Later", "🚀 Quick Start"]
            )
            
            if time_option == "🚀 Start Now":
                start_time = current_jakarta
            elif time_option == "🚀 Quick Start":
                quick_minutes = st.selectbox("⏱️ Start in:", [5, 15, 30, 60])
                start_time = current_jakarta + timedelta(minutes=quick_minutes)
                st.info(f"⏰ Will start in {quick_minutes} minutes at {start_time.strftime('%H:%M WIB')}")
            else:
                date_input = st.date_input("📅 Date", value=current_jakarta.date())
                time_input = st.time_input("🕐 Time", value=current_jakarta.time())
                start_time = JAKARTA_TZ.localize(datetime.combine(date_input, time_input))
            
            privacy = st.selectbox("🔒 Privacy", ["public", "unlisted", "private"])
            quality = st.selectbox("📺 Quality", ["720p", "1080p", "480p", "360p"])
        
        with col2:
            st.subheader("📸 Thumbnail Upload")
            thumbnail_file = st.file_uploader(
                "Upload Thumbnail (JPG/PNG)",
                type=['jpg', 'jpeg', 'png'],
                help="Max 2MB, recommended 1280x720"
            )
            
            if thumbnail_file:
                st.image(thumbnail_file, caption="Thumbnail Preview", width=300)
                
                # Save uploaded thumbnail
                thumbnail_path = f"temp_thumbnail_{int(time.time())}.jpg"
                with open(thumbnail_path, "wb") as f:
                    f.write(thumbnail_file.getbuffer())
            else:
                thumbnail_path = None
        
        if st.button("🚀 Create Broadcast", type="primary"):
            with st.spinner("Creating broadcast..."):
                result = create_youtube_broadcast(
                    service, title, description, start_time, privacy, quality, thumbnail_path
                )
                
                if isinstance(result, tuple):
                    st.error(f"❌ {result[1]}")
                else:
                    st.success("✅ Broadcast created successfully!")
                    
                    # Store new stream data in session state
                    st.session_state.new_stream_data = result
                    
                    # Display info
                    st.info(f"🔗 **YouTube URL:** {result['youtube_url']}")
                    st.info(f"🔑 **Stream Key:** {result['stream_key']}")
                    st.info(f"📡 **RTMP URL:** {result['rtmp_url']}")
                    
                    if result.get('thumbnail_uploaded'):
                        st.success("📸 Thumbnail uploaded successfully!")
                    
                    # Auto-switch to Add New Stream tab
                    st.success("🎯 **Next Step:** Go to 'Add New Stream' tab to configure your streaming settings!")
                    st.info("💡 **Tip:** Your stream key and settings are already prepared for you!")
                
                # Cleanup temp thumbnail
                if thumbnail_path and os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
    
    # Tab 2: Add New Stream
    with tabs[1]:
        st.header("➕ Add New Stream Configuration")
        
        # Check if we have new stream data from broadcast creation
        if st.session_state.new_stream_data:
            stream_data = st.session_state.new_stream_data
            
            st.success("🎉 **Broadcast Created Successfully!** Configure your streaming settings below:")
            
            # Display broadcast info
            with st.expander("📋 Broadcast Information", expanded=True):
                col1, col2 = st.columns(2)
                with col1:
                    st.info(f"**Title:** {stream_data['title']}")
                    st.info(f"**Quality:** {stream_data['quality']}")
                    st.info(f"**Privacy:** {stream_data['privacy_status']}")
                with col2:
                    st.info(f"**YouTube URL:** {stream_data['youtube_url']}")
                    st.info(f"**Broadcast ID:** {stream_data['broadcast_id']}")
            
            st.divider()
            
            # Stream configuration form
            st.subheader("⚙️ Stream Configuration")
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("📁 Video Source")
                
                source_type = st.radio(
                    "Source Type",
                    ["📁 Video File", "📹 Camera/Webcam", "🖥️ Screen Capture"]
                )
                
                video_source = None
                if source_type == "📁 Video File":
                    video_files = get_video_files()
                    if video_files:
                        selected_video = st.selectbox(
                            "Select Video File",
                            options=range(len(video_files)),
                            format_func=lambda x: f"{video_files[x]['name']} ({video_files[x]['size']})"
                        )
                        video_source = video_files[selected_video]['path']
                        st.success(f"✅ Selected: {video_files[selected_video]['name']}")
                    else:
                        st.warning("⚠️ No video files found in current directory")
                        st.info("📁 Upload video files to the current directory")
                
                elif source_type == "📹 Camera/Webcam":
                    camera_index = st.number_input("Camera Index", min_value=0, max_value=10, value=0)
                    video_source = f"/dev/video{camera_index}"
                    st.info(f"📹 Camera source: {video_source}")
                
                else:  # Screen Capture
                    st.info("🖥️ Screen capture will capture your entire screen")
                    video_source = ":0.0+0,0"
            
            with col2:
                st.subheader("⚙️ Stream Settings")
                
                # Pre-filled stream information (read-only)
                st.text_input("🔑 Stream Key", value=stream_data['stream_key'], disabled=True)
                st.text_input("📡 RTMP URL", value=stream_data['rtmp_url'], disabled=True)
                st.text_input("📺 Quality", value=stream_data['quality'], disabled=True)
                
                # Duration setting
                duration_enabled = st.checkbox("⏱️ Set Duration Limit")
                duration_minutes = None
                if duration_enabled:
                    duration_minutes = st.number_input("Duration (minutes)", min_value=1, max_value=480, value=60)
                    st.info(f"⏰ Stream will stop automatically after {duration_minutes} minutes")
                
                # Additional settings
                st.subheader("🔧 Advanced Settings")
                auto_start = st.checkbox("🚀 Auto-start after saving", value=True)
                save_config = st.checkbox("💾 Save configuration for later", value=True)
            
            st.divider()
            
            # Action buttons
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if st.button("💾 Save Stream Configuration", type="primary"):
                    if video_source:
                        # Save stream configuration
                        streams = load_streams()
                        stream_config = {
                            'title': stream_data['title'],
                            'broadcast_id': stream_data['broadcast_id'],
                            'stream_key': stream_data['stream_key'],
                            'rtmp_url': stream_data['rtmp_url'],
                            'youtube_url': stream_data['youtube_url'],
                            'quality': stream_data['quality'],
                            'start_time': stream_data['start_time'],
                            'created_at': get_jakarta_time().isoformat(),
                            'status': 'configured',
                            'video_source': video_source,
                            'source_type': source_type,
                            'duration_minutes': duration_minutes,
                            'privacy_status': stream_data['privacy_status']
                        }
                        streams.append(stream_config)
                        save_streams(streams)
                        
                        st.success("✅ Stream configuration saved successfully!")
                        
                        # Clear the new stream data
                        st.session_state.new_stream_data = None
                        
                        if auto_start:
                            st.info("🚀 Starting stream automatically...")
                            start_streaming_with_recovery(
                                stream_data['rtmp_url'],
                                stream_data['stream_key'],
                                video_source,
                                stream_data['quality'],
                                duration_minutes
                            )
                    else:
                        st.error("❌ Please select a video source first!")
            
            with col2:
                if st.button("🚀 Start Streaming Now"):
                    if video_source and not st.session_state.streaming_active:
                        start_streaming_with_recovery(
                            stream_data['rtmp_url'],
                            stream_data['stream_key'],
                            video_source,
                            stream_data['quality'],
                            duration_minutes
                        )
                    elif st.session_state.streaming_active:
                        st.error("🔴 Streaming is already active!")
                    else:
                        st.error("❌ Please select a video source first!")
            
            with col3:
                if st.button("❌ Cancel Configuration"):
                    st.session_state.new_stream_data = None
                    st.success("✅ Configuration cancelled")
                    st.rerun()
        
        else:
            # No new stream data - show manual configuration
            st.info("📝 **No recent broadcast found.** Create a broadcast first or manually configure a stream.")
            
            st.subheader("🔧 Manual Stream Configuration")
            
            col1, col2 = st.columns(2)
            
            with col1:
                manual_title = st.text_input("📝 Stream Title")
                manual_stream_key = st.text_input("🔑 Stream Key")
                manual_rtmp_url = st.text_input("📡 RTMP URL")
                manual_quality = st.selectbox("📺 Quality", ["720p", "1080p", "480p", "360p"])
            
            with col2:
                manual_youtube_url = st.text_input("🔗 YouTube URL")
                manual_broadcast_id = st.text_input("📺 Broadcast ID")
                
                if st.button("💾 Save Manual Configuration"):
                    if all([manual_title, manual_stream_key, manual_rtmp_url]):
                        streams = load_streams()
                        manual_config = {
                            'title': manual_title,
                            'broadcast_id': manual_broadcast_id or 'manual',
                            'stream_key': manual_stream_key,
                            'rtmp_url': manual_rtmp_url,
                            'youtube_url': manual_youtube_url,
                            'quality': manual_quality,
                            'start_time': get_jakarta_time().isoformat(),
                            'created_at': get_jakarta_time().isoformat(),
                            'status': 'manual',
                            'privacy_status': 'unknown'
                        }
                        streams.append(manual_config)
                        save_streams(streams)
                        st.success("✅ Manual stream configuration saved!")
                    else:
                        st.error("❌ Please fill in all required fields!")
    
    # Tab 3: Manage Streams
    with tabs[2]:
        st.header("📺 Manage Live Streams")
        
        streams = load_streams()
        
        if not streams:
            st.info("📝 No streams configured yet. Create your first broadcast!")
        else:
            # Display streams in cards
            for i, stream in enumerate(streams):
                with st.container():
                    col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
                    
                    with col1:
                        st.subheader(f"🎬 {stream['title']}")
                        st.write(f"**Quality:** {stream['quality']}")
                        st.write(f"**Status:** {stream.get('status', 'unknown')}")
                        
                        # Calculate time until start
                        try:
                            start_time = datetime.fromisoformat(stream['start_time'])
                            if start_time.tzinfo is None:
                                start_time = JAKARTA_TZ.localize(start_time)
                            
                            current_time = get_jakarta_time()
                            time_diff = start_time - current_time
                            
                            if time_diff.total_seconds() > 0:
                                hours, remainder = divmod(int(time_diff.total_seconds()), 3600)
                                minutes, _ = divmod(remainder, 60)
                                st.write(f"⏰ **Starts in:** {hours}h {minutes}m")
                            else:
                                st.write("🔴 **Status:** Live/Past")
                        except:
                            st.write("⏰ **Time:** Unknown")
                    
                    with col2:
                        st.write(f"**Start Time:**")
                        try:
                            start_time = datetime.fromisoformat(stream['start_time'])
                            st.write(start_time.strftime('%d/%m/%Y %H:%M WIB'))
                        except:
                            st.write("Unknown")
                        
                        st.write(f"**Created:**")
                        try:
                            created_time = datetime.fromisoformat(stream['created_at'])
                            st.write(created_time.strftime('%d/%m %H:%M'))
                        except:
                            st.write("Unknown")
                    
                    with col3:
                        if stream.get('youtube_url'):
                            if st.button(f"🔗 Open YouTube", key=f"youtube_{i}"):
                                st.markdown(f"[Open Stream]({stream['youtube_url']})")
                        
                        if st.button(f"📋 Copy Stream Key", key=f"copy_{i}"):
                            st.code(stream['stream_key'])
                        
                        if st.button(f"⚙️ Edit Config", key=f"edit_{i}"):
                            st.session_state.new_stream_data = stream
                            st.info("✅ Stream loaded for editing. Go to 'Add New Stream' tab.")
                    
                    with col4:
                        if st.button(f"🗑️ Delete", key=f"delete_{i}", type="secondary"):
                            streams.pop(i)
                            save_streams(streams)
                            st.rerun()
                    
                    st.divider()
    
    # Tab 4: Start Streaming
    with tabs[3]:
        st.header("🎥 Start Streaming")
        
        streams = load_streams()
        if not streams:
            st.warning("⚠️ No streams configured. Create a broadcast and configure a stream first!")
            st.info("💡 Go to 'Create Broadcast' tab to get started.")
        else:
            # Stream selection
            st.subheader("📺 Select Stream to Start")
            
            selected_stream_idx = st.selectbox(
                "Choose Stream",
                options=range(len(streams)),
                format_func=lambda x: f"{streams[x]['title']} ({streams[x]['quality']}) - {streams[x].get('status', 'unknown')}"
            )
            
            selected_stream = streams[selected_stream_idx]
            
            # Display stream info
            col1, col2 = st.columns(2)
            
            with col1:
                st.info(f"🎬 **Title:** {selected_stream['title']}")
                st.info(f"📺 **Quality:** {selected_stream['quality']}")
                st.info(f"🔗 **YouTube:** {selected_stream.get('youtube_url', 'N/A')}")
            
            with col2:
                st.info(f"📡 **RTMP:** {selected_stream['rtmp_url']}")
                st.info(f"🔑 **Stream Key:** {selected_stream['stream_key'][:20]}...")
                st.info(f"📊 **Status:** {selected_stream.get('status', 'unknown')}")
            
            st.divider()
            
            # Video source and streaming controls
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("📁 Video Source")
                
                # Check if stream has pre-configured video source
                if selected_stream.get('video_source'):
                    st.success(f"✅ **Pre-configured:** {selected_stream.get('source_type', 'Unknown')}")
                    st.info(f"📁 **Source:** {selected_stream['video_source']}")
                    video_source = selected_stream['video_source']
                    
                    # Option to override
                    override_source = st.checkbox("🔄 Override video source")
                    if override_source:
                        source_type = st.radio("New Source Type", ["📁 Video File", "📹 Camera/Webcam"])
                        if source_type == "📁 Video File":
                            video_files = get_video_files()
                            if video_files:
                                selected_video = st.selectbox(
                                    "Select Video File",
                                    options=range(len(video_files)),
                                    format_func=lambda x: f"{video_files[x]['name']} ({video_files[x]['size']})"
                                )
                                video_source = video_files[selected_video]['path']
                        else:
                            camera_index = st.number_input("Camera Index", min_value=0, max_value=10, value=0)
                            video_source = f"/dev/video{camera_index}"
                else:
                    # No pre-configured source
                    source_type = st.radio("Source Type", ["📁 Video File", "📹 Camera/Webcam"])
                    
                    if source_type == "📁 Video File":
                        video_files = get_video_files()
                        if video_files:
                            selected_video = st.selectbox(
                                "Select Video File",
                                options=range(len(video_files)),
                                format_func=lambda x: f"{video_files[x]['name']} ({video_files[x]['size']})"
                            )
                            video_source = video_files[selected_video]['path']
                        else:
                            st.warning("⚠️ No video files found")
                            video_source = None
                    else:
                        camera_index = st.number_input("Camera Index", min_value=0, max_value=10, value=0)
                        video_source = f"/dev/video{camera_index}"
            
            with col2:
                st.subheader("⚙️ Streaming Controls")
                
                # Duration setting
                duration_enabled = st.checkbox("⏱️ Set Duration Limit", 
                                             value=bool(selected_stream.get('duration_minutes')))
                duration_minutes = None
                if duration_enabled:
                    default_duration = selected_stream.get('duration_minutes', 60)
                    duration_minutes = st.number_input("Duration (minutes)", 
                                                     min_value=1, max_value=480, 
                                                     value=default_duration)
                
                st.divider()
                
                # Streaming buttons
                if video_source and not st.session_state.streaming_active:
                    if st.button("🚀 Start Streaming", type="primary", use_container_width=True):
                        start_streaming_with_recovery(
                            selected_stream['rtmp_url'],
                            selected_stream['stream_key'],
                            video_source,
                            selected_stream['quality'],
                            duration_minutes
                        )
                
                elif st.session_state.streaming_active:
                    st.error("🔴 **Streaming is currently active!**")
                    if st.button("⏹️ Stop Current Stream", type="secondary", use_container_width=True):
                        stop_streaming()
                
                else:
                    st.warning("⚠️ Please select a video source first")
                
                # Quick actions
                st.divider()
                st.subheader("🔧 Quick Actions")
                
                col_a, col_b = st.columns(2)
                with col_a:
                    if st.button("🔗 Open YouTube", use_container_width=True):
                        if selected_stream.get('youtube_url'):
                            st.markdown(f"[Open Stream]({selected_stream['youtube_url']})")
                        else:
                            st.warning("No YouTube URL available")
                
                with col_b:
                    if st.button("📋 Show Stream Key", use_container_width=True):
                        st.code(selected_stream['stream_key'])

if __name__ == "__main__":
    main()
