from urllib.parse import urlencode

import scrapy

class LinkedJobsSpider(scrapy.Spider):
    """Patched for FO_Intelligence_Agent: the upstream spider hardcoded
    keywords=python&location=United States and paginated without limit. This version
    takes `-a keywords=...` (required), `-a location=...` (optional), and caps pagination
    via `-a max_pages=N` (default 1 = 25 results) — each request costs ScrapeOps proxy
    credits, so unbounded pagination is not something a per-lead lookup should do.
        scrapy crawl linkedin_jobs -a keywords="Acme Family Office" -a max_pages=2
    """
    name = "linkedin_jobs"

    def __init__(self, keywords=None, location=None, max_pages=1, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not keywords:
            raise ValueError("linkedin_jobs requires -a keywords=<search terms>")
        self.max_pages = int(max_pages)
        params = {'keywords': keywords}
        if location:
            params['location'] = location
        self.api_url = (
            'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?'
            + urlencode(params) + '&start='
        )

    def start_requests(self):
        first_job_on_page = 0
        first_url = self.api_url + str(first_job_on_page)
        yield scrapy.Request(url=first_url, callback=self.parse_job, meta={'first_job_on_page': first_job_on_page, 'page': 1})

    async def start(self):
        # Scrapy >=2.13 no longer calls start_requests() automatically — it calls this
        # async generator instead. Delegate to keep one source of truth for the requests.
        for request in self.start_requests():
            yield request


    def parse_job(self, response):
        first_job_on_page = response.meta['first_job_on_page']
        page = response.meta['page']

        job_item = {}
        jobs = response.css("li")

        num_jobs_returned = len(jobs)
        print("******* Num Jobs Returned *******")
        print(num_jobs_returned)
        print('*****')

        for job in jobs:

            job_item['job_title'] = job.css("h3::text").get(default='not-found').strip()
            job_item['job_detail_url'] = job.css(".base-card__full-link::attr(href)").get(default='not-found').strip()
            job_item['job_listed'] = job.css('time::text').get(default='not-found').strip()

            job_item['company_name'] = job.css('h4 a::text').get(default='not-found').strip()
            job_item['company_link'] = job.css('h4 a::attr(href)').get(default='not-found')
            job_item['company_location'] = job.css('.job-search-card__location::text').get(default='not-found').strip()
            yield job_item


        if num_jobs_returned > 0 and page < self.max_pages:
            first_job_on_page = int(first_job_on_page) + 25
            next_url = self.api_url + str(first_job_on_page)
            yield scrapy.Request(url=next_url, callback=self.parse_job, meta={'first_job_on_page': first_job_on_page, 'page': page + 1})

    

