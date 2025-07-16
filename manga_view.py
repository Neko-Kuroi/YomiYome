import os
import zipfile
import rarfile
import hashlib
import tempfile
import requests
from PIL import Image
from natsort import natsorted
import streamlit as st
from io import BytesIO
import time
import json
import urllib.parse
import base64
import shutil

MAX_UPLOADS_LENGTH = 12
CACHE_SIZE_LIMIT_MB = 270 # 新しい定数: キャッシュサイズの上限 (MB)
IMAGES_PER_LOAD = 5 # 一度に読み込む画像の枚数

def get_dir_size(path):
    """Calculates the total size of a directory in bytes."""
    total = 0
    if os.path.exists(path):
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp): # シンボリックリンクを複数回カウントしないようにする
                    total += os.path.getsize(fp)
    return total

def manage_cache_size():
    """
    Checks the total size of cached manga archives and their extracted content,
    and deletes unread ones if the total exceeds CACHE_SIZE_LIMIT_MB.
    """
    cache_dir = os.path.join(tempfile.gettempdir(), "manga_cache")
    if not os.path.exists(cache_dir):
        return

    total_cache_size_bytes = 0
    manga_items_by_hash = {}

    current_reading_url = st.session_state.get('selected_manga_url')
    current_reading_hash = hashlib.md5(current_reading_url.encode()).hexdigest() if current_reading_url else None

    for item_name in os.listdir(cache_dir):
        item_path = os.path.join(cache_dir, item_name)
        if os.path.isfile(item_path):
            file_ext = os.path.splitext(item_name)[-1].lower()
            if file_ext in ['.zip', '.cbz', '.rar', '.cbr']:
                url_hash = os.path.splitext(item_name)[0]
                if url_hash not in manga_items_by_hash:
                    manga_items_by_hash[url_hash] = {
                        'archive_path': item_path,
                        'extracted_path': os.path.join(cache_dir, url_hash + "_extracted"),
                        'total_size': 0,
                        'mtime': os.path.getmtime(item_path),
                        'is_currently_reading': (url_hash == current_reading_hash)
                    }
        elif os.path.isdir(item_path) and item_name.endswith("_extracted"):
            url_hash = item_name.replace("_extracted", "")
            if url_hash not in manga_items_by_hash:
                manga_items_by_hash[url_hash] = {
                    'archive_path': None,
                    'extracted_path': item_path,
                    'total_size': 0,
                    'mtime': os.path.getmtime(item_path),
                    'is_currently_reading': (url_hash == current_reading_hash)
                }
            else:
                manga_items_by_hash[url_hash]['extracted_path'] = item_path
                manga_items_by_hash[url_hash]['mtime'] = max(manga_items_by_hash[url_hash].get('mtime', 0), os.path.getmtime(item_path)) 

    for url_hash, item_info in manga_items_by_hash.items():
        current_item_total_size = 0
        if item_info.get('archive_path') and os.path.exists(item_info['archive_path']):
            current_item_total_size += os.path.getsize(item_info['archive_path'])
        if item_info.get('extracted_path') and os.path.exists(item_info['extracted_path']):
            current_item_total_size += get_dir_size(item_info['extracted_path'])
        item_info['total_size'] = current_item_total_size
        total_cache_size_bytes += current_item_total_size

    cache_limit_bytes = CACHE_SIZE_LIMIT_MB * 1024 * 1024

    if total_cache_size_bytes > cache_limit_bytes:
        sorted_manga_items = sorted(manga_items_by_hash.values(), key=lambda x: (x['is_currently_reading'], x['mtime']))
        for item_info in sorted_manga_items:
            if total_cache_size_bytes <= cache_limit_bytes:
                break
            if not item_info['is_currently_reading']:
                try:
                    if item_info.get('archive_path') and os.path.exists(item_info['archive_path']):
                        os.remove(item_info['archive_path'])
                    if item_info.get('extracted_path') and os.path.exists(item_info['extracted_path']):
                        shutil.rmtree(item_info['extracted_path'])
                    total_cache_size_bytes -= item_info['total_size']
                except Exception as e:
                    st.error(f"キャッシュアイテムの削除に失敗しました: {e}")

# ==========================
# Utility Functions
# ==========================
def is_valid_image(filename):
    return filename.lower().endswith(('.jpg', '.jpeg', '.png'))

def get_safe_filename(original_filename, index=None):
    ext = os.path.splitext(original_filename)[1].lower()
    if len(original_filename) > 50:
        hash_name = hashlib.md5(original_filename.encode()).hexdigest()
        return f"{hash_name}_{index}{ext}" if index is not None else f"{hash_name}{ext}"
    return original_filename

def get_cache_path(url):
    base_dir = os.path.join(tempfile.gettempdir(), "manga_cache")
    os.makedirs(base_dir, exist_ok=True)
    file_hash = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(base_dir, file_hash)

def cleanup_cache():
    try:
        cache_dir = os.path.join(tempfile.gettempdir(), "manga_cache")
        if os.path.exists(cache_dir):
            current_time = time.time()
            for item in os.listdir(cache_dir):
                item_path = os.path.join(cache_dir, item)
                if os.path.isfile(item_path):
                    if current_time - os.path.getmtime(item_path) > 86400 / 48:
                        os.remove(item_path)
    except Exception:
        pass

def download_file(url, save_path, max_size_mb=240):
    if os.path.exists(save_path):
        return True
    placeholder = st.empty()
    progress_bar = placeholder.progress(0.0)
    try:
        response = requests.get(url, stream=True, timeout=(10, 60))
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        max_bytes = max_size_mb * 1024 * 1024
        if total_size > 0 and total_size > max_bytes:
            st.warning(f"ファイルサイズが {max_size_mb}MB を超えています（{total_size / (1024*1024):.2f}MB）。ダウンロードを中止します。")
            placeholder.empty()
            return False
        bytes_downloaded = 0
        with tempfile.NamedTemporaryFile(delete=False, dir=os.path.dirname(save_path)) as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    bytes_downloaded += len(chunk)
                    if total_size == 0 and bytes_downloaded > max_bytes:
                        st.warning(f"ダウンロードサイズが {max_size_mb}MB を超えたため中止します。")
                        placeholder.empty()
                        return False
                    tmp_file.write(chunk)
                    if total_size > 0:
                        progress = bytes_downloaded / total_size
                    else:
                        progress = min((bytes_downloaded / max_bytes), 1.0)
                    progress_bar.progress(progress)
            tmp_file_path = tmp_file.name
        os.rename(tmp_file_path, save_path)
        placeholder.empty()
        return True
    except requests.exceptions.RequestException as e:
        st.error(f"ダウンロードエラー: {e}")
        placeholder.empty()
        return False

def extract_archive(archive_path, extract_to, is_rar=False):
    os.makedirs(extract_to, exist_ok=True)
    image_files = []
    try:
        archive = rarfile.RarFile(archive_path) if is_rar else zipfile.ZipFile(archive_path)
        for index, file_info in enumerate(archive.infolist()):
            filename = file_info.filename
            if is_valid_image(filename):
                try:
                    safe_filename = get_safe_filename(filename, index)
                    extracted_path = os.path.join(extract_to, safe_filename)
                    if not os.path.exists(extracted_path):
                        archive.extract(file_info, extract_to)
                        original_path = os.path.join(extract_to, filename)
                        if original_path != extracted_path and os.path.exists(original_path):
                            os.rename(original_path, extracted_path)
                    if os.path.exists(extracted_path):
                        image_files.append(extracted_path)
                except Exception as e:
                    if "File name too long" in str(e):
                        st.warning(f"エラー: {filename} のファイル名が長すぎます。スキップします。")
                    else:
                        st.warning(f"エラー: {filename} の解凍に失敗しました ({e})。スキップします。")
    except (zipfile.BadZipFile, rarfile.BadRarFile) as e:
        st.error(f"エラー: 無効なアーカイブファイルです: {e}")
    except Exception as e:
        st.error(f"エラー: アーカイブの解凍中に予期せぬエラーが発生しました: {e}")
    return natsorted(image_files)

@st.cache_resource(show_spinner=False)
def load_image_as_bytesio(image_path):
    if not os.path.exists(image_path):
        # st.error(f"画像ファイルが見つかりません: {image_path}")
        return None
    try:
        img = Image.open(image_path)
        img.thumbnail((800, 1200)) # Resize for efficient display
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer
    except Exception as e:
        st.error(f"画像の読み込みに失敗しました: {e}")
        return None

def get_filename_from_url(url):
    filename = os.path.basename(url).split('?')[0]
    try:
        return urllib.parse.unquote(filename, encoding='utf-8')
    except Exception:
        return filename

def shorten_tinyurl(long_url):
    api_url = "https://tinyurl.com/api-create.php"
    params = {"url": long_url}
    try:
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException:
        return long_url # 短縮に失敗した場合は元のURLを返す

# ==========================
# Session State Management
# ==========================
def initialize_session_state():
    """
    Initializes Streamlit session state variables if they don't exist.
    """
    session_defaults = {
        'manga_urls': [],
        'current_mode': 'list',
        'selected_manga_url': None,
        'image_files': [],
        'last_manga_url': None,
        'last_loaded_share_data': None,
        'copy_success_message_displayed': False,
        'show_sharing': False,
        'num_images_to_display': IMAGES_PER_LOAD,  # ▼▼▼ 追加 ▼▼▼
        'show_video': False  # ▼▼▼ この行を追加 ▼▼▼
    }
    for key, value in session_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

# (add_manga_url, remove_manga_url, ... , show_sharing_options などの他の関数は変更なし)
# ...
def add_manga_url(url, title=""):
    max_uploads_length = MAX_UPLOADS_LENGTH
    if len(st.session_state.manga_urls) >= max_uploads_length:
        return "full"
    if url and url not in [manga['url'] for manga in st.session_state.manga_urls]:
        file_ext = os.path.splitext(url)[-1].lower()
        if file_ext in ['.zip', '.cbz', '.rar', '.cbr']:
            if not title:
                title = get_filename_from_url(url)
            manga_info = {'url': url, 'title': title, 'added_time': time.time()}
            st.session_state.manga_urls.append(manga_info)
            return True
    return False

def remove_manga_url(url):
    st.session_state.manga_urls = [manga for manga in st.session_state.manga_urls if manga['url'] != url]

def export_manga_list():
    if st.session_state.manga_urls:
        manga_data = {'manga_urls': st.session_state.manga_urls, 'export_time': time.time()}
        return json.dumps(manga_data, ensure_ascii=False, indent=2)
    return None

def import_manga_list(json_str):
    max_uploads_length = MAX_UPLOADS_LENGTH
    try:
        manga_data = json.loads(json_str)
        if 'manga_urls' in manga_data:
            existing_urls = [manga['url'] for manga in st.session_state.manga_urls]
            new_count = 0
            for manga in manga_data['manga_urls']:
                if len(st.session_state.manga_urls) >= max_uploads_length:
                    st.warning("マンガリストが最大数に達したため、残りのマンガはインポートされませんでした。")
                    break
                if manga['url'] not in existing_urls:
                    st.session_state.manga_urls.append(manga)
                    new_count += 1
            return new_count
    except Exception as e:
        st.error(f"インポートエラー: {e}")
    return 0

def generate_share_url_param():
    if st.session_state.manga_urls:
        manga_data = {'manga_urls': st.session_state.manga_urls, 'export_time': time.time()}
        json_str = json.dumps(manga_data, ensure_ascii=False)
        return base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
    return None

def load_from_share_url(encoded_data):
    try:
        json_str = base64.b64decode(encoded_data).decode('utf-8')
        return import_manga_list(json_str)
    except Exception as e:
        st.error(f"共有データの読み込みエラー: {e}")
        return 0

def show_sharing_options():
    st.write("**🌐 URL共有**")
    share_data_param = generate_share_url_param()
    if share_data_param:
        #base_app_url = "https://huggingface.co/spaces/kuroiikimono/yomima_show_multi_share"
        #base_app_url = "https://huggingface.co/spaces/Kuroinekomono/yomima2_share_v"
        base_app_url = "https://yukctpkmjpskvgpvjecidp.streamlit.app/"
        parsed_base_url = urllib.parse.urlparse(base_app_url)
        query_dict = urllib.parse.parse_qs(parsed_base_url.query)
        query_dict['share'] = [share_data_param]
        new_query_string = urllib.parse.urlencode(query_dict, doseq=True)
        full_share_url = urllib.parse.urlunparse(
            (parsed_base_url.scheme, parsed_base_url.netloc, parsed_base_url.path,
             parsed_base_url.params, new_query_string, parsed_base_url.fragment)
        )
        if full_share_url:
            tiny_url = shorten_tinyurl(full_share_url)
            st.caption("以下のテキストボックスに、共有可能なURLが表示されています。**すべて選択し、コピーしてください。**")
            st.text_area(
                "共有URL",
                tiny_url,
                height=90,
                key="generated_share_url_display_area",
                help="このURL全体をコピーして他の人と共有してください。"
            )

def show_manga_list():
    st.title("📚 マンガライブラリ")
    st.subheader("📥 新しいマンガを追加")
    col1, col2 = st.columns([3, 1])
    with col1:
        new_url = st.text_input("マンガアーカイブURL (.zip, .cbz, .rar, .cbr)", placeholder="https://example.com/manga.zip")
    with col2:
        st.write("")
        if st.button("追加", type="primary"):
            result = add_manga_url(new_url)
            if result is True:
                st.success("マンガが追加されました！")
                st.rerun()
            elif result == "full":
                st.error("マンガリストが最大数に達しました。")
            else:
                st.error("無効なURLまたは既に追加済みです")
    
    if st.session_state.manga_urls:
        st.subheader("📖 マンガリスト")
        for i, manga in enumerate(st.session_state.manga_urls):
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                st.write(f"**{manga['title']}**")
                st.caption(f"URL: unlisted")
            with col2:
                if st.button("読む", key=f"read_{i}"):
                    st.session_state.selected_manga_url = manga['url']
                    st.session_state.current_mode = 'reader'
                    st.rerun()
            with col3:
                if st.button("削除", key=f"delete_{i}"):
                    remove_manga_url(manga['url'])
                    st.rerun()
            st.divider()
        if st.button("🔗 URL共有"):
            st.session_state.show_sharing = not st.session_state.show_sharing
        if st.session_state.show_sharing:
            show_sharing_options()
    else:
        st.info("まだマンガが追加されていません。")

# ====================================================================
# ▼▼▼ ここから show_manga_reader 関数を大幅に変更します ▼▼▼
# ====================================================================
def show_manga_reader():
    """
    Displays the manga reader in a vertical scrolling (long-strip) format,
    loading images incrementally.
    """
    url = st.session_state.selected_manga_url
    if not url:
        st.session_state.current_mode = 'list'
        st.rerun()
        return

    # --- トップナビゲーション ---
    top_cols = st.columns([1, 3])
    with top_cols[0]:
        if st.button("↩️ マンガリストに戻る"):
            st.session_state.current_mode = 'list'
            # 閲覧状態をリセット
            st.session_state.num_images_to_display = IMAGES_PER_LOAD
            st.rerun()
            return

    manga_title = next((manga['title'] for manga in st.session_state.manga_urls if manga['url'] == url), get_filename_from_url(url))
    
    st.write(f"📖 {manga_title}")

    # --- アーカイブのダウンロードと展開 ---
    file_ext = os.path.splitext(url)[-1].lower()
    if file_ext not in ['.zip', '.cbz', '.rar', '.cbr']:
        st.error("URLは.zip, .cbz, .rar, .cbr ファイルである必要があります。")
        return

    archive_path = get_cache_path(url) + file_ext
    extract_path = get_cache_path(url) + "_extracted"

    if not download_file(url, archive_path):
        return

    is_rar = archive_path.endswith(('.rar', '.cbr'))
    image_files = extract_archive(archive_path, extract_path, is_rar)

    if not image_files:
        st.warning("🧟 有効な画像ファイルが見つかりませんでした。")
        return
        
    # マンガが切り替わったら、表示枚数をリセット
    if st.session_state.get('last_manga_url') != url:
        st.session_state.image_files = image_files
        st.session_state.last_manga_url = url
        st.session_state.num_images_to_display = IMAGES_PER_LOAD # 初期枚数にリセット
    
    st.session_state.image_files = image_files
    total_pages = len(st.session_state.image_files)

    # --- 画像の遅延読み込みと表示 ---
    # 表示する画像の枚数を決定
    num_to_display = min(st.session_state.num_images_to_display, total_pages)

    for i in range(num_to_display):
        img_path = st.session_state.image_files[i]
        img_bytes = load_image_as_bytesio(img_path)
        if img_bytes:
            st.image(img_bytes, use_container_width=True)

    # --- ページ下部のナビゲーション（もっと読み込む） ---
    if num_to_display < total_pages:
        st.write("---")
        
        # 「もっと読み込む」ボタン用のコンテナ
        bottom_container = st.container()
        
        with bottom_container:
            # 進行状況の表示
            st.write(f"表示中: {num_to_display} / {total_pages} 枚")
            
            # ボタンを中央に配置するための列
            b_col1, b_col2, b_col3 = st.columns([1,2,1])
            with b_col2:
                if st.button("▼ もっと読み込む", use_container_width=True):
                    st.session_state.num_images_to_display += IMAGES_PER_LOAD
                    st.rerun()
            
                if st.button("🐻 すべて読み込む", use_container_width=True):
                    st.session_state.num_images_to_display = total_pages
                    st.rerun()
    else:
        st.success("🎉 すべてのページを読み込みました！")
    # ▼▼▼ ここからが追加部分 ▼▼▼
    st.divider() # 区切り線を入れて見た目を整えます

    # ボタンを中央に配置するために列を使用します
    bottom_cols = st.columns([1, 2, 1])
    with bottom_cols[1]:
        # 上のボタンと区別するために、ユニークな `key` を設定します
        if st.button("↩️ マンガリストに戻る", key="back_to_list_bottom", use_container_width=True):
            st.session_state.current_mode = 'list'
            # 閲覧状態をリセット
            st.session_state.num_images_to_display = IMAGES_PER_LOAD
            st.rerun()
            # ここでは return は不要です
        st.divider() # 区切り線を入れて見た目を整えます    
        st.write(f"📖 {manga_title}")
    # ▲▲▲ 追加部分はここまで ▲▲▲

# ====================================================================
# ▲▲▲ show_manga_reader 関数の変更はここまで ▲▲▲
# ====================================================================

def main():
    """
    Main function to run the Streamlit application.
    """
    st.set_page_config(layout="wide") # ページレイアウトをワイドに設定
    
    initialize_session_state()
    
    # 起動時のキャッシュ管理
    manage_cache_size() 
    cleanup_cache()

    if 'share' in st.query_params:
        current_share_data = st.query_params['share']
        if isinstance(current_share_data, list):
            current_share_data = current_share_data[0]
        if st.session_state.last_loaded_share_data != current_share_data:
            st.session_state.last_loaded_share_data = current_share_data
            new_count = load_from_share_url(current_share_data)
            if new_count > 0:
                st.success(f"{new_count}個の新しいマンガが共有URLから追加されました！")
                st.rerun()
            else:
                st.info("共有URLからの新しいマンガはありませんでした。")

    st.markdown("""
        <style>
        .stImage > img { border-radius: 0 !important; }
        .stButton > button { margin-top: 5px; margin-bottom: 5px; }
        div[data-testid="stVerticalBlock"] > [style*="flex-direction: column;"] > [data-testid="stVerticalBlock"] {
            gap: 0; /* 画像間の余白をなくす */
        }
        </style>
    """, unsafe_allow_html=True)
    
    if st.session_state.current_mode == 'list':
        st.code("""今日こそマンガよみましょうね。🤸
ブラウザだけでよめますよ。https://huggingface.co/spaces/kuroiikimono/yomima_show
        """, language='python')
        st.write("せつめい ⤵")
        
        # 「せつめい動画を見る」ボタンを設置
        if st.button("せつめい動画を見る ▶️"):
            # ボタンが押されたら、動画の表示状態を反転させる (True -> False, False -> True)
            st.session_state.show_video = not st.session_state.show_video

        # st.session_state.show_video が True の場合のみ、動画を表示
        if st.session_state.show_video:
            st.video("https://youtu.be/A2tzHbcNMmw")
        
        st.write("👶 複数の .zip または .rar ファイルのマンガ URL を管理してマンガをよみましょう。")
    
    if st.session_state.current_mode == 'list':
        show_manga_list()
    elif st.session_state.current_mode == 'reader':
        show_manga_reader()

if __name__ == "__main__":
    main()
