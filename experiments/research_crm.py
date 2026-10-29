import requests
from bs4 import BeautifulSoup

def research_monday_crm():
    urls = [
        "https://monday.com/crm",
        "https://support.monday.com/hc/en-us/articles/360014762359-The-monday-sales-CRM"
    ]
    
    findings = []
    for url in urls:
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                # Extract text from common tags
                text = ' '.join([p.get_text() for p in soup.find_all(['p', 'li', 'h2', 'h3'])])
                findings.append(f"Source: {url}\nContent Snippet: {text[:2000]}...")
            else:
                findings.append(f"Failed to fetch {url}: Status {response.status_code}")
        except Exception as e:
            findings.append(f"Error fetching {url}: {e}")
            
    return "\n\n".join(findings)

if __name__ == "__main__":
    print(research_monday_crm())
