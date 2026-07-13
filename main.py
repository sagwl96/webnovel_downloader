import asyncio
import aiohttp
import requests
from bs4 import BeautifulSoup
from ebooklib import epub
import time
import sys
import os
import json
import re
from urllib.parse import urljoin
import random

# -------------------- CONFIGURATION --------------------
MIN_CHAPTER_WORDS = 500
MAX_CHAPTER_ATTEMPTS = 3
CONCURRENT_REQUESTS = 5          # safe concurrency level
REQUEST_DELAY_RANGE = (0.3, 1.0) # random seconds before each request

# Tags we completely ignore
SKIP_TAGS = {'script', 'style', 'meta', 'link', 'noscript', 'iframe', 'svg', 'img', 'br', 'hr'}
# Classes/IDs that strongly suggest navigation or boilerplate
NON_CONTENT_PATTERNS = re.compile(
    r'(nav|menu|footer|comment|sidebar|header|widget|related|pagination|next|prev|chapter-list)',
    re.IGNORECASE
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}


# -------------------- HELPER FUNCTIONS --------------------
def chapter_word_count(content_html):
    """Count readable chapter words, ignoring HTML markup."""
    return len(BeautifulSoup(content_html, 'html.parser').get_text(" ", strip=True).split())


def find_main_content(soup):
    """
    Return the BeautifulSoup tag that most likely contains the chapter text.
    Returns None if no good candidate is found.
    """
    body = soup.find('body')
    if not body:
        return None

    total_words = len(body.get_text(separator=' ', strip=True).split())

    best_tag = None
    best_score = -1

    for tag in body.find_all(True):
        if tag.name in SKIP_TAGS:
            continue

        cls_id = ' '.join(tag.get('class', []) + [tag.get('id', '')])
        if NON_CONTENT_PATTERNS.search(cls_id):
            continue

        text = tag.get_text(separator=' ', strip=True)
        word_count = len(text.split())

        if total_words > 0 and word_count > 0.9 * total_words:
            continue

        p_count = len(tag.find_all('p'))

        link_text = ' '.join(a.get_text(separator=' ', strip=True) for a in tag.find_all('a'))
        link_word_count = len(link_text.split())
        link_ratio = link_word_count / word_count if word_count else 0

        score = word_count + (p_count * 20) - (link_ratio * 50)

        if score > best_score:
            best_score = score
            best_tag = tag

    return best_tag


def extract_chapter_number(title):
    """Extract the numeric chapter number from a title string."""
    match = re.search(r'chapter\s*(\d+)', title, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def ensure_correct_chapter_title(title, idx):
    """
    Ensure the title contains the correct chapter number (idx+1).
    If the number is wrong or missing, correct it.
    """
    expected = idx + 1
    # Try to find a number after 'chapter'
    match = re.search(r'(chapter\s*)(\d+)', title, re.IGNORECASE)
    if match:
        prefix = match.group(1)
        num = int(match.group(2))
        if num != expected:
            # Replace the number with the correct one
            new_title = re.sub(r'(chapter\s*)(\d+)', r'\g<1>' + str(expected), title, flags=re.IGNORECASE)
            return new_title
        else:
            return title
    else:
        # No chapter number found – prepend one
        return f"Chapter {expected}: {title}"


def clean_duplicate_title(content_html, chapter_title):
    """Remove any existing heading that matches the chapter title."""
    soup = BeautifulSoup(content_html, 'html.parser')
    for h in soup.find_all(['h1', 'h2', 'h3']):
        if h.get_text(strip=True) == chapter_title:
            h.decompose()
            break
    return str(soup)


# -------------------- ASYNC DOWNLOADER --------------------
async def download_chapter(session, semaphore, idx, chapter_url, cache_dir):
    """Download one chapter, with caching and auto‑detection."""
    chap_file = os.path.join(cache_dir, f"chap_{idx+1}.json")

    # 1) Check cache
    if os.path.exists(chap_file):
        try:
            with open(chap_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Sanity checks: word count AND chapter number
            if chapter_word_count(data['content']) > MIN_CHAPTER_WORDS:
                # Correct the title if needed (in memory)
                corrected_title = ensure_correct_chapter_title(data['title'], idx)
                print(f"Chapter {idx+1} cached, skipping.")
                return corrected_title, data['content'], None
        except (OSError, json.JSONDecodeError, KeyError):
            pass   # fall through to re‑download

    chapter_title = f"Chapter {idx+1}"
    content_html = None
    last_error = None
    last_word_count = 0

    # 2) Acquire semaphore to limit concurrency
    async with semaphore:
        # Add random jitter to avoid burst requests
        await asyncio.sleep(random.uniform(*REQUEST_DELAY_RANGE))

        # 3) Retry loop
        for attempt in range(1, MAX_CHAPTER_ATTEMPTS + 1):
            try:
                async with session.get(chapter_url, timeout=10) as resp:
                    resp.raise_for_status()
                    text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = f"Network error: {e}"
                await asyncio.sleep(1.5 * attempt)
                continue

            soup = BeautifulSoup(text, 'lxml')

            # Try known container
            content_div = soup.find('div', id='chapter-content')
            if content_div:
                candidate = str(content_div).replace('class="chapter-c"', '')
                wc = chapter_word_count(candidate)
                if wc > MIN_CHAPTER_WORDS:
                    title_tag = soup.find('a', class_='chr-title')
                    raw_title = title_tag.text.strip() if title_tag else f"Chapter {idx+1}"
                    # Ensure correct chapter number
                    chapter_title = ensure_correct_chapter_title(raw_title, idx)
                    content_html = candidate
                    break
                else:
                    last_error = f"Known container had only {wc} words"
                    last_word_count = wc

            # Fallback auto‑detection
            main_tag = find_main_content(soup)
            if main_tag:
                candidate = str(main_tag)
                wc = chapter_word_count(candidate)
                if wc > MIN_CHAPTER_WORDS:
                    title_tag = (soup.find('a', class_='chr-title') or
                                 soup.find('h1') or soup.find('h2'))
                    raw_title = title_tag.text.strip() if title_tag else f"Chapter {idx+1}"
                    chapter_title = ensure_correct_chapter_title(raw_title, idx)
                    content_html = candidate
                    break
                else:
                    last_error = f"Auto-detection found only {wc} words"
                    last_word_count = wc
            else:
                last_error = "Auto-detection failed"

            await asyncio.sleep(1.0)

        # 4) If all attempts failed, return failure info
        if content_html is None:
            return None, None, {
                'chapter_number': idx+1,
                'title': chapter_title,
                'url': chapter_url,
                'attempts': MAX_CHAPTER_ATTEMPTS,
                'word_count': last_word_count,
                'reason': last_error,
            }

        # 5) Remove duplicate title inside content
        content_html = clean_duplicate_title(content_html, chapter_title)

        # 6) Save to cache (with corrected title)
        with open(chap_file, 'w', encoding='utf-8') as f:
            json.dump({'title': chapter_title, 'content': content_html}, f)

        return chapter_title, content_html, None


async def download_all_chapters(all_chapter_links, cache_dir):
    """Orchestrate concurrent downloads of all chapters."""
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        tasks = []
        for idx, url in enumerate(all_chapter_links):
            tasks.append(download_chapter(session, semaphore, idx, url, cache_dir))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    chapters_data = []
    failed_chapters = []
    for idx, result in enumerate(results):
        if isinstance(result, Exception):
            failed_chapters.append({
                'chapter_number': idx+1,
                'title': f"Chapter {idx+1}",
                'url': all_chapter_links[idx],
                'reason': str(result)
            })
            continue
        title, content, failure = result
        if content is None:
            failed_chapters.append(failure)
        else:
            chapters_data.append((title, content))

    return chapters_data, failed_chapters


# -------------------- MAIN EPUB BUILDING --------------------
def create_epub(novel_url, output_filename):
    # Setup cache directory
    cache_dir = output_filename.replace('.epub', '_chapters')
    os.makedirs(cache_dir, exist_ok=True)
    failed_report_file = os.path.join(cache_dir, 'failed_chapters.json')
    print(f"Using local cache folder: {cache_dir}/")

    # EPUB book object
    book = epub.EpubBook()
    book.set_identifier(output_filename)
    book.set_title(output_filename.replace('.epub', ''))
    book.set_language('en')

    # ---- Fetch cover image ----
    print("Fetching novel data and cover image...")
    try:
        main_page_response = requests.get(novel_url, headers=HEADERS, timeout=10)
        main_soup = BeautifulSoup(main_page_response.text, 'html.parser')
        book_div = main_soup.find('div', class_='book')
        if book_div:
            img_tag = book_div.find('img')
            if img_tag and 'src' in img_tag.attrs:
                img_url = urljoin(novel_url, img_tag['src'])
                img_response = requests.get(img_url, headers=HEADERS, timeout=10)
                book.set_cover("cover.jpg", img_response.content)
                print("Cover image downloaded successfully.")
    except Exception as e:
        print(f"Could not fetch cover image: {e}")

    # ---- Fetch chapter list (sequential, only a few pages) ----
    print("Fetching chapter list...")
    all_chapter_links = []
    page_num = 1
    while True:
        url = f"{novel_url}?page={page_num}&per-page=50"
        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
        except requests.exceptions.RequestException:
            print(f"Network error on page {page_num}. Exiting loop.")
            break

        if response.status_code != 200:
            print(f"Failed to fetch page {page_num}. Exiting loop.")
            break

        soup = BeautifulSoup(response.text, 'html.parser')
        chapter_lists = soup.find_all('ul', class_='list-chapter')
        if not chapter_lists:
            break

        links = []
        for cl in chapter_lists:
            links.extend(cl.find_all('a'))
        if not links:
            break

        new_links_found = False
        for link in links:
            if 'href' not in link.attrs:
                continue
            chapter_url = urljoin(novel_url, link['href'])
            if chapter_url not in all_chapter_links:
                all_chapter_links.append(chapter_url)
                new_links_found = True

        if not new_links_found:
            print(f"\nReached the end of pagination at page {page_num}.")
            break

        print(f"Scanning page {page_num}... Found {len(all_chapter_links)} unique chapters so far.", end="\r")
        page_num += 1
        time.sleep(0.5)

    if not all_chapter_links:
        print("\nNo chapters found. Check the URL or site structure.")
        sys.exit()

    print(f"\nFound {len(all_chapter_links)} chapters total. Starting download phase (concurrent, {CONCURRENT_REQUESTS} at a time)...")

    # ---- Download all chapters asynchronously ----
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    chapters_data, failed_chapters = loop.run_until_complete(
        download_all_chapters(all_chapter_links, cache_dir)
    )
    loop.close()

    # ---- Add chapters to EPUB ----
    chapters_epub = []
    for idx, (title, content) in enumerate(chapters_data):
        c = epub.EpubHtml(title=title, file_name=f'chap_{idx+1}.xhtml', lang='en')
        c.content = f'<h2>{title}</h2>{content}'
        book.add_item(c)
        chapters_epub.append(c)

    print("\nAll chapters processed. Compiling EPUB file now...")

    # ---- Finalise EPUB ----
    book.toc = tuple(chapters_epub)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    style = 'BODY {color: black;}'
    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)

    book.spine = ['nav'] + chapters_epub

    epub.write_epub(output_filename, book, {})
    print(f"Done. Saved to {output_filename}")

    # ---- Failed chapters report ----
    if failed_chapters:
        with open(failed_report_file, 'w', encoding='utf-8') as f:
            json.dump(failed_chapters, f, ensure_ascii=False, indent=2)
        print("\n--- Failed Chapters Report ---")
        print(f"These chapters failed. Details saved to {failed_report_file}.")
        print("Run the script again to retry downloading them.")
        for failed in failed_chapters:
            print(f"Chapter {failed['chapter_number']}: {failed['title']} — {failed['reason']}")
    else:
        if os.path.exists(failed_report_file):
            os.remove(failed_report_file)
        print("\nAll chapters successfully compiled.")


if __name__ == "__main__":
    NOVEL_URL = "https://novelfull.com/library-of-heavens-path.html"
    OUTPUT_FILENAME = "Library_of_Heavens_Path.epub"
    create_epub(NOVEL_URL, OUTPUT_FILENAME)