import json
import scrapy

class LinkedCompanySpider(scrapy.Spider):
    """Patched for FO_Intelligence_Agent: the upstream spider hardcoded two demo company
    URLs. This version takes companies via `-a company=<slug-or-url>` (comma-separated
    for multiple), e.g.:
        scrapy crawl linkedin_company_profile -a company=usebraintrust
        scrapy crawl linkedin_company_profile -a company=https://www.linkedin.com/company/usebraintrust
    """
    name = "linkedin_company_profile"

    custom_settings = {
        'FEEDS': { 'data/%(name)s_%(time)s.jsonl': { 'format': 'jsonlines',}}
    }

    def __init__(self, company=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not company:
            raise ValueError("linkedin_company_profile requires -a company=<slug-or-url>")
        self.company_pages = [self._to_url(c.strip()) for c in company.split(',') if c.strip()]

    @staticmethod
    def _to_url(value: str) -> str:
        if 'linkedin.com/company/' in value:
            slug = value.split('linkedin.com/company/', 1)[1].strip('/').split('/')[0].split('?')[0]
        else:
            slug = value.strip('/')
        return f'https://www.linkedin.com/company/{slug}'

    def start_requests(self):

        company_index_tracker = 0

        #uncomment below if reading the company urls from a file instead of the self.company_pages array
        # self.readUrlsFromJobsFile()

        first_url = self.company_pages[company_index_tracker]

        yield scrapy.Request(url=first_url, callback=self.parse_response, meta={'company_index_tracker': company_index_tracker})

    async def start(self):
        # Scrapy >=2.13 no longer calls start_requests() automatically — it calls this
        # async generator instead. Delegate to keep one source of truth for the requests.
        for request in self.start_requests():
            yield request


    def parse_response(self, response):
        company_index_tracker = response.meta['company_index_tracker']
        print('***************')
        print('****** Scraping page ' + str(company_index_tracker+1) + ' of ' + str(len(self.company_pages)))
        print('***************')

        company_item = {}

        company_item['name'] = response.css('.top-card-layout__entity-info h1::text').get(default='not-found').strip()
        company_item['summary'] = response.css('.top-card-layout__entity-info h4 span::text').get(default='not-found').strip()

        try:
            ## all company details 
            company_details = response.css('.core-section-container__content .mb-2')

            #industry line
            company_industry_line = company_details[1].css('.text-md::text').getall()
            company_item['industry'] = company_industry_line[1].strip()

            #company size line
            company_size_line = company_details[2].css('.text-md::text').getall()
            company_item['size'] = company_size_line[1].strip()

            #company founded
            company_size_line = company_details[5].css('.text-md::text').getall()
            company_item['founded'] = company_size_line[1].strip()
        except IndexError:
            print("Error: Skipped Company - Some details missing")

        yield company_item
        

        company_index_tracker = company_index_tracker + 1

        if company_index_tracker <= (len(self.company_pages)-1):
            next_url = self.company_pages[company_index_tracker]

            yield scrapy.Request(url=next_url, callback=self.parse_response, meta={'company_index_tracker': company_index_tracker})

    



    def readUrlsFromJobsFile(self):
        self.company_pages = []
        with open('jobs.json') as file:
            jobsFromFile = json.load(file)

            for job in jobsFromFile:
                if job['company_link'] != 'not-found':
                    self.company_pages.append(job['company_link'])
            
        #remove any duplicate links - to prevent spider from shutting down on duplicate
        self.company_pages = list(set(self.company_pages))
            
