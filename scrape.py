#!/usr/bin/env python3
"""
Crisp.nl Product Scraper - Find products on sale/cheap
Simple web scraper to get product data from Crisp.nl
"""

import requests
from bs4 import BeautifulSoup
import json
import re
import time
from urllib.parse import urljoin, urlparse
import csv
from datetime import datetime
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class CrispScraper:
    def __init__(self):
        self.base_url = "https://crisp.nl"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        self.products = []
        
    def get_page(self, url, retries=3):
        """Get a page with retry logic"""
        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"Failed to fetch {url} after {retries} attempts")
                    return None
    
    def extract_price(self, price_text):
        """Extract numerical price from text"""
        if not price_text:
            return None
        
        # Remove currency symbols and find numbers
        price_match = re.search(r'(\d+[,.]?\d*)', price_text.replace('€', '').replace(',', '.'))
        if price_match:
            return float(price_match.group(1))
        return None
    
    def is_on_sale(self, product):
        """Determine if a product is on sale based on various indicators"""
        text_lower = (product.get('title', '') + ' ' + product.get('description', '')).lower()
        
        # Sale indicators
        sale_keywords = [
            'korting', 'sale', 'aanbieding', 'actie', 'voordeel', 
            'nu voor', 'was', 'bespaar', 'nu', '%'
        ]
        
        # Check for discount percentage
        discount_match = re.search(r'(\d+)%', text_lower)
        if discount_match:
            product['discount_percentage'] = int(discount_match.group(1))
            return True
            
        # Check for sale keywords
        for keyword in sale_keywords:
            if keyword in text_lower:
                return True
                
        # Check if there are both original and sale prices
        if product.get('original_price') and product.get('sale_price'):
            return True
            
        return False
    
    def scrape_product_from_element(self, element):
        """Extract product information from a BeautifulSoup element"""
        product = {}
        
        try:
            # Try to find product title
            title_elem = element.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']) or \
                        element.find(class_=re.compile(r'title|name|product', re.I)) or \
                        element.find(attrs={'data-testid': re.compile(r'title|name', re.I)})
            
            if title_elem:
                product['title'] = title_elem.get_text(strip=True)
            
            # Try to find price information
            price_elements = element.find_all(class_=re.compile(r'price|cost|amount', re.I)) + \
                           element.find_all(attrs={'data-testid': re.compile(r'price', re.I)}) + \
                           element.find_all(text=re.compile(r'€\s*\d+'))
            
            prices = []
            for price_elem in price_elements:
                if hasattr(price_elem, 'get_text'):
                    price_text = price_elem.get_text(strip=True)
                else:
                    price_text = str(price_elem).strip()
                
                if '€' in price_text:
                    price = self.extract_price(price_text)
                    if price:
                        prices.append(price)
            
            if prices:
                if len(prices) > 1:
                    # If multiple prices, assume first is sale price, second is original
                    product['sale_price'] = min(prices)
                    product['original_price'] = max(prices)
                else:
                    product['price'] = prices[0]
            
            # Try to find description
            desc_elem = element.find(class_=re.compile(r'description|desc|summary', re.I))
            if desc_elem:
                product['description'] = desc_elem.get_text(strip=True)
            
            # Try to find image
            img_elem = element.find('img')
            if img_elem:
                src = img_elem.get('src') or img_elem.get('data-src')
                if src:
                    product['image'] = urljoin(self.base_url, src)
            
            # Try to find product link
            link_elem = element.find('a')
            if link_elem and link_elem.get('href'):
                product['link'] = urljoin(self.base_url, link_elem['href'])
            
            return product if product.get('title') and (product.get('price') or product.get('sale_price')) else None
            
        except Exception as e:
            logger.warning(f"Error extracting product: {e}")
            return None
    
    def scrape_products_page(self, url):
        """Scrape products from a specific page"""
        logger.info(f"Scraping: {url}")
        response = self.get_page(url)
        
        if not response:
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        products = []
        
        # Look for JSON-LD structured data first
        json_scripts = soup.find_all('script', type='application/ld+json')
        for script in json_scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get('@type') == 'Product':
                    product = {
                        'title': data.get('name'),
                        'description': data.get('description'),
                        'price': self.extract_price(str(data.get('offers', {}).get('price', ''))),
                        'link': url,
                        'source': 'json-ld'
                    }
                    if product['title'] and product['price']:
                        products.append(product)
            except (json.JSONDecodeError, AttributeError):
                continue
        
        # If no JSON-LD found, try to find product containers
        if not products:
            # Common selectors for product containers
            product_selectors = [
                '[class*="product"]',
                '[class*="item"]',
                '[data-testid*="product"]',
                'article',
                '.card',
                '[class*="tile"]'
            ]
            
            for selector in product_selectors:
                elements = soup.select(selector)
                if elements:
                    logger.info(f"Found {len(elements)} elements with selector: {selector}")
                    for element in elements[:10]:  # Limit to prevent overwhelming
                        product = self.scrape_product_from_element(element)
                        if product:
                            products.append(product)
                    
                    if products:  # If we found products, don't try other selectors
                        break
        
        return products
    
    def find_product_pages(self):
        """Find product listing pages"""
        urls_to_scrape = [
            f"{self.base_url}/onze-producten",
            f"{self.base_url}/",
        ]
        
        # Try to find category pages
        try:
            response = self.get_page(f"{self.base_url}/onze-producten")
            if response:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Look for category links
                links = soup.find_all('a', href=True)
                for link in links:
                    href = link['href']
                    if any(keyword in href.lower() for keyword in ['product', 'categorie', 'category']):
                        full_url = urljoin(self.base_url, href)
                        if full_url not in urls_to_scrape:
                            urls_to_scrape.append(full_url)
                            
        except Exception as e:
            logger.warning(f"Error finding category pages: {e}")
        
        return urls_to_scrape[:5]  # Limit to first 5 to be respectful
    
    def scrape_all_products(self):
        """Main scraping function"""
        logger.info("Starting Crisp.nl product scraping...")
        
        urls = self.find_product_pages()
        logger.info(f"Found {len(urls)} URLs to scrape")
        
        all_products = []
        
        for url in urls:
            products = self.scrape_products_page(url)
            all_products.extend(products)
            
            # Be respectful - add delay between requests
            time.sleep(1)
        
        # Remove duplicates based on title
        seen_titles = set()
        unique_products = []
        for product in all_products:
            if product['title'] not in seen_titles:
                seen_titles.add(product['title'])
                unique_products.append(product)
        
        self.products = unique_products
        logger.info(f"Found {len(self.products)} unique products")
        
        return self.products
    
    def find_sale_products(self):
        """Filter products that are on sale or cheap"""
        if not self.products:
            self.scrape_all_products()
        
        sale_products = []
        
        for product in self.products:
            if self.is_on_sale(product):
                product['on_sale'] = True
                sale_products.append(product)
        
        # Sort by discount percentage if available, otherwise by price
        sale_products.sort(key=lambda x: (-x.get('discount_percentage', 0), x.get('price', x.get('sale_price', 999))))
        
        return sale_products
    
    def save_to_csv(self, products, filename=None):
        """Save products to CSV file"""
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"crisp_products_{timestamp}.csv"
        
        if not products:
            logger.warning("No products to save")
            return
        
        fieldnames = ['title', 'price', 'sale_price', 'original_price', 'discount_percentage', 
                     'description', 'link', 'image', 'on_sale', 'source']
        
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            for product in products:
                # Fill in missing fields
                row = {field: product.get(field, '') for field in fieldnames}
                writer.writerow(row)
        
        logger.info(f"Saved {len(products)} products to {filename}")
        return filename
    
    def print_sale_products(self, limit=20):
        """Print sale products to console"""
        sale_products = self.find_sale_products()
        
        if not sale_products:
            print("❌ No products on sale found!")
            return
        
        print(f"\n🎉 Found {len(sale_products)} products on sale!")
        print("=" * 80)
        
        for i, product in enumerate(sale_products[:limit], 1):
            print(f"\n{i}. {product['title']}")
            
            if product.get('discount_percentage'):
                print(f"   💰 {product['discount_percentage']}% KORTING!")
            
            if product.get('sale_price') and product.get('original_price'):
                savings = product['original_price'] - product['sale_price']
                print(f"   💸 Was: €{product['original_price']:.2f} → Nu: €{product['sale_price']:.2f} (bespaar €{savings:.2f})")
            elif product.get('price'):
                print(f"   💰 Prijs: €{product['price']:.2f}")
            
            if product.get('description'):
                desc = product['description'][:100] + "..." if len(product['description']) > 100 else product['description']
                print(f"   📝 {desc}")
            
            if product.get('link'):
                print(f"   🔗 {product['link']}")
        
        if len(sale_products) > limit:
            print(f"\n... en nog {len(sale_products) - limit} meer!")


def main():
    """Main execution function"""
    scraper = CrispScraper()
    
    try:
        # Scrape all products
        print("🔍 Searching for products at Crisp.nl...")
        products = scraper.scrape_all_products()
        
        if products:
            # Save all products
            csv_file = scraper.save_to_csv(products)
            print(f"📊 All products saved to: {csv_file}")
            
            # Show sale products
            scraper.print_sale_products()
            
            # Save sale products separately
            sale_products = scraper.find_sale_products()
            if sale_products:
                sale_csv = scraper.save_to_csv(sale_products, f"crisp_sale_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
                print(f"🏷️  Sale products saved to: {sale_csv}")
        else:
            print("❌ No products found. The website structure might have changed.")
            print("💡 Try checking if Crisp.nl requires app access or has anti-scraping measures.")
            
    except KeyboardInterrupt:
        print("\n⏹️  Scraping interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        print(f"❌ An error occurred: {e}")


if __name__ == "__main__":
    main()
