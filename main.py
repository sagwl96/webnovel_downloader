import requests
from bs4 import BeautifulSoup
from ebooklib import epub
import time
import sys
import os
import json

def create_epub(novel_url, output_filename):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    # Setup local cache directory
    cache_dir = output_filename.replace('.epub', '_chapters')
    os.makedirs(cache_dir, exist_ok=True)
    print(f"Using local cache folder: {cache_dir}/")

    book = epub.EpubBook()
    book.set_identifier(output_filename)
    book.set_title(output_filename.replace('.epub', ''))
    book.set_language('en')
    
    chapters_epub = []
    failed_chapters = []
    page_num = 1
    base_url = 'https://novelfull.com'
    
    print("Fetching novel data and cover image...")
    
    try:
        main_page_response = requests.get(novel_url, headers=headers, timeout=10)
        main_soup = BeautifulSoup(main_page_response.text, 'html.parser')
        
        book_div = main_soup.find('div', class_='book')
        if book_div:
            img_tag = book_div.find('img')
            if img_tag and 'src' in img_tag.attrs:
                img_url = img_tag['src']
                if not img_url.startswith('http'):
                    img_url = base_url + img_url
                    
                img_response = requests.get(img_url, headers=headers, timeout=10)
                book.set_cover("cover.jpg", img_response.content)
                print("Cover image downloaded successfully.")
    except Exception as e:
        print(f"Could not fetch cover image: {e}")

    print("Fetching chapter list. This will take about a minute.")
    
    all_chapter_links = []
    while True:
        url = f"{novel_url}?page={page_num}&per-page=50"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
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
            chapter_url = base_url + link['href']
            if chapter_url not in all_chapter_links:
                all_chapter_links.append(chapter_url)
                new_links_found = True
                
        if not new_links_found:
            print(f"\nReached the end of pagination at page {page_num}.")
            break
            
        print(f"Scanning page {page_num}... Found {len(all_chapter_links)} unique chapters so far.", end="\r")
        
        page_num += 1
        time.sleep(1)

    if not all_chapter_links:
        print("\nNo chapters found. Check the URL or site structure.")
        sys.exit()

    print(f"\nFound {len(all_chapter_links)} chapters total. Starting download phase.")

    for idx, chapter_url in enumerate(all_chapter_links):
        chap_file = os.path.join(cache_dir, f"chap_{idx+1}.json")
        
        # Check if chapter is already downloaded
        if os.path.exists(chap_file):
            print(f"Chapter {idx + 1}/{len(all_chapter_links)} found in cache. Skipping download.", end="\r")
            with open(chap_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                chapter_title = data['title']
                content_html = data['content']
        else:
            print(f"\nDownloading chapter {idx + 1}/{len(all_chapter_links)}...")
            try:
                response = requests.get(chapter_url, headers=headers, timeout=10)
            except requests.exceptions.RequestException:
                print(f"Network error on chapter {idx + 1}. Skipping.")
                failed_chapters.append(f"Chapter {idx + 1} (Network Error)")
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            title_tag = soup.find('a', class_='chr-title')
            chapter_title = title_tag.text.strip() if title_tag else f"Chapter {idx + 1}"
            
            content_div = soup.find('div', id='chapter-content')
            if not content_div:
                print(f"Could not find content for {chapter_title}. Skipping.")
                failed_chapters.append(chapter_title)
                continue
                
            content_html = str(content_div).replace('class="chapter-c"', '')
            
            # Save the clean data to the local cache
            with open(chap_file, 'w', encoding='utf-8') as f:
                json.dump({'title': chapter_title, 'content': content_html}, f)
            
            time.sleep(1.5)
            
        # Add to EPUB memory
        c = epub.EpubHtml(title=chapter_title, file_name=f'chap_{idx+1}.xhtml', lang='en')
        c.content = f'<h2>{chapter_title}</h2>{content_html}'
        book.add_item(c)
        chapters_epub.append(c)
        
    print("\n\nAll chapters processed. Compiling EPUB file now...")

    book.toc = tuple(chapters_epub)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    style = 'BODY {color: white;}'
    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)
    
    book.spine = ['nav'] + chapters_epub
    
    epub.write_epub(output_filename, book, {})
    print(f"Done. Saved to {output_filename}")
    
    if failed_chapters:
        print("\n--- Failed Chapters Report ---")
        print("These chapters failed. Run the script again to retry downloading them.")
        for failed in failed_chapters:
            print(failed)
    else:
        print("\nAll chapters successfully compiled.")

if __name__ == "__main__":
    NOVEL_URL = "https://novelfull.com/coiling-dragon.html"
    OUTPUT_FILENAME = "Coiling_Dragon.epub"
    
    create_epub(NOVEL_URL, OUTPUT_FILENAME)