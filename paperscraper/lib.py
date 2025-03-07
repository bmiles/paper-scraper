import os
import re
import pybtex
from pybtex.bibtex import BibTeXEngine
from .headers import get_header
from .utils import ThrottledClientSession
from .scraper import Scraper
import asyncio
import re
import sys
import logging
from .log_formatter import CustomFormatter


def clean_upbibtex(bibtex):
    # WTF Semantic Scholar?
    mapping = {
        "None": "article",
        "Article": "article",
        "JournalArticle": "article",
        "Review": "article",
        "Book": "book",
        "BookSection": "inbook",
        "ConferencePaper": "inproceedings",
        "Conference": "inproceedings",
        "Dataset": "misc",
        "Dissertation": "phdthesis",
        "Journal": "article",
        "Patent": "patent",
        "Preprint": "article",
        "Report": "techreport",
        "Thesis": "phdthesis",
        "WebPage": "misc",
        "Plain": "article",
    }

    if "@None" in bibtex:
        return bibtex.replace("@None", "@article")
    # new format check
    match = re.findall(r"@\['(.*)'\]", bibtex)
    if len(match) == 0:
        match = re.findall(r"@(.*)\{", bibtex)
        bib_type = match[0]
        current = f"@{match[0]}"
    else:
        bib_type = match[0]
        current = f"@['{bib_type}']"
    for k, v in mapping.items():
        # can have multiple
        if k in bib_type:
            bibtex = bibtex.replace(current, f"@{v}")
            break
    return bibtex


def format_bibtex(bibtex, key):
    # WOWOW This is hard to use
    from pybtex.database import parse_string
    from pybtex.style.formatting import unsrtalpha

    style = unsrtalpha.Style()
    try:
        bd = parse_string(clean_upbibtex(bibtex), "bibtex")
    except Exception as e:
        return "Ref " + key
    try:
        entry = style.format_entry(label="1", entry=bd.entries[key])
        return entry.text.render_as("text")
    except Exception:
        return bd.entries[key].fields["title"]


async def likely_pdf(response):
    try:
        text = await response.text()
        if "Invalid article ID" in text:
            return False
        if "No paper" in text:
            return False
    except UnicodeDecodeError:
        return True
    return True


async def arxiv_to_pdf(arxiv_id, path, session):
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    # download
    async with session.get(url, allow_redirects=True) as r:
        if r.status != 200 or not await likely_pdf(r):
            raise RuntimeError(f"No paper with arxiv id {arxiv_id}")
        with open(path, "wb") as f:
            f.write(await r.read())


async def link_to_pdf(url, path, session):
    # download
    pdf_link = None
    async with session.get(url, allow_redirects=True) as r:
        if r.status != 200:
            raise RuntimeError(f"Unable to download {url}, status code {r.status}")
        if "pdf" in r.headers["Content-Type"]:
            with open(path, "wb") as f:
                f.write(await r.read())
            return
        else:
            # try to find a pdf link
            html_text = await r.text()
            # should have pdf somewhere (could not be at end)
            epdf_link = re.search(r'href="(.*\.epdf)"', html_text)
            if epdf_link is None:
                pdf_link = re.search(r'href="(.*pdf.*)"', html_text)
                # try to find epdf link
                if pdf_link is None:
                    raise RuntimeError(f"No PDF link found for {url}")
            else:
                # strip the epdf
                pdf_link = epdf_link.group(1).replace("epdf", "pdf")
    try:
        if pdf_link is None:
            raise RuntimeError(f"No PDF link found for {url}")
        result = await link_to_pdf(pdf_link, path, session)
    except TypeError:
        raise RuntimeError(f"Malformed URL {pdf_link} -- {url}")


async def find_pmc_pdf_link(pmc_id, session):
    url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}"
    async with session.get(url) as r:
        if r.status != 200:
            raise RuntimeError(f"No paper with pmc id {pmc_id}. {url} {r.status}")
        html_text = await r.text()
        pdf_link = re.search(r'href="(.*\.pdf)"', html_text)
        if pdf_link is None:
            raise RuntimeError(f"No PDF link found for pmc id {pmc_id}. {url}")
        return f"https://www.ncbi.nlm.nih.gov{pdf_link.group(1)}"


async def pubmed_to_pdf(pubmed_id, path, session):
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_id}/"

    async with session.get(url) as r:
        if r.status != 200:
            raise RuntimeError(
                f"Error fetching PMC ID for PubMed ID {pubmed_id}. {r.status}"
            )
        html_text = await r.text()
        pmc_id_match = re.search(r"PMC\d+", html_text)
        if pmc_id_match is None:
            raise RuntimeError(f"No PMC ID found for PubMed ID {pubmed_id}.")
        pmc_id = pmc_id_match.group(0)
    pmc_id = pmc_id[3:]
    return await pmc_to_pdf(pmc_id, path, session)


async def pmc_to_pdf(pmc_id, path, session):
    pdf_url = await find_pmc_pdf_link(pmc_id, session)
    async with session.get(pdf_url, allow_redirects=True) as r:
        if r.status != 200 or not await likely_pdf(r):
            raise RuntimeError(f"No paper with pmc id {pmc_id}. {pdf_url} {r.status}")
        with open(path, "wb") as f:
            f.write(await r.read())


async def doi_to_pdf(doi, path, session):
    # worth a shot
    try:
        return await link_to_pdf(f"https://doi.org/{doi}", path, session)
    except Exception as e:
        pass
    base = os.environ.get("DOI2PDF")
    if base is None:
        raise RuntimeError("No DOI2PDF environment variable set")
    if base[-1] == "/":
        base = base[:-1]
    url = f"{base}/{doi}"
    # get to iframe thing
    async with session.get(url, allow_redirects=True) as iframe_r:
        if iframe_r.status != 200:
            raise RuntimeError(f"No paper with doi {doi}")
        # get pdf url by regex
        # looking for button onclick
        try:
            pdf_url = re.search(
                r"location\.href='(.*?download=true)'", await iframe_r.text()
            ).group(1)
        except AttributeError:
            raise RuntimeError(f"No paper with doi {doi}")
    # can be relative or absolute
    if pdf_url.startswith("//"):
        pdf_url = f"https:{pdf_url}"
    else:
        pdf_url = f"{base}{pdf_url}"
    # download
    async with session.get(pdf_url, allow_redirects=True) as r:
        with open(path, "wb") as f:
            f.write(await r.read())


async def arxiv_scraper(paper, path, session):
    if "ArXiv" not in paper["externalIds"]:
        return False
    arxiv_id = paper["externalIds"]["ArXiv"]
    await arxiv_to_pdf(arxiv_id, path, session)
    return True


async def pmc_scraper(paper, path, session):
    if "PubMedCentral" not in paper["externalIds"]:
        return False
    pmc_id = paper["externalIds"]["PubMedCentral"]
    await pmc_to_pdf(pmc_id, path, session)
    return True


async def pubmed_scraper(paper, path, session):
    if "PubMed" not in paper["externalIds"]:
        return False
    pubmed_id = paper["externalIds"]["PubMed"]
    await pubmed_to_pdf(pubmed_id, path, session)
    return True


async def openaccess_scraper(paper, path, session):
    if not paper["isOpenAccess"]:
        return False
    url = paper["openAccessPdf"]["url"]
    await link_to_pdf(url, path, session)
    return True


async def doi_scraper(paper, path, session):
    if "DOI" not in paper["externalIds"]:
        return False
    doi = paper["externalIds"]["DOI"]
    await doi_to_pdf(doi, path, session)
    return True


async def local_scraper(paper, path):
    return True


def default_scraper():
    scraper = Scraper()
    scraper.register_scraper(arxiv_scraper, attach_session=True, rate_limit=30 / 60)
    scraper.register_scraper(pmc_scraper, rate_limit=30 / 60, attach_session=True)
    scraper.register_scraper(pubmed_scraper, rate_limit=30 / 60, attach_session=True)
    scraper.register_scraper(
        openaccess_scraper, attach_session=True, priority=5, rate_limit=45 / 60
    )
    scraper.register_scraper(doi_scraper, attach_session=True, priority=0)
    scraper.register_scraper(local_scraper, attach_session=False, priority=11)
    return scraper


async def a_search_papers(
    query,
    limit=10,
    pdir=os.curdir,
    semantic_scholar_api_key=None,
    _paths=None,
    _limit=100,
    _offset=0,
    logger=None,
    year=None,
    verbose=False,
    scraper=None,
    batch_size=10,
):
    if not os.path.exists(pdir):
        os.mkdir(pdir)
    if logger is None:
        logger = logging.getLogger("paper-scraper")
        logger.setLevel(logging.ERROR)
        if verbose:
            logger.setLevel(logging.DEBUG)
            ch = logging.StreamHandler()
            ch.setFormatter(CustomFormatter())
            logger.addHandler(ch)
    endpoint = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "fields": ",".join(
            [
                "citationStyles",
                "externalIds",
                "url",
                "openAccessPdf",
                "year",
                "isOpenAccess",
                "influentialCitationCount",
                "tldr",
                "title",
            ]
        ),
        "limit": _limit,
        "offset": _offset,
    }
    if year is not None:
        # need to really make sure year is correct
        year = year.strip()
        if "-" in year:
            # make sure start/end are valid
            try:
                start, end = year.split("-")
                if int(start) < int(end):
                    params["year"] = year
            except ValueError:
                pass
        if "year" not in params:
            logger.warning(f"Could not parse year {year}")
    if _paths is None:
        paths = {}
    else:
        paths = _paths
    if scraper is None:
        scraper = default_scraper()
    ssheader = get_header()
    if semantic_scholar_api_key is not None:
        ssheader["x-api-key"] = semantic_scholar_api_key
    else:
        # check if its in the environment
        try:
            ssheader["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
        except KeyError:
            pass
    async with ThrottledClientSession(
        rate_limit=90 if "x-api-key" in ssheader else 15 / 60, headers=ssheader
    ) as ss_session:
        async with ss_session.get(url=endpoint, params=params) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Error searching papers: {response.status} {response.reason} {await response.text()}"
                )
            data = await response.json()
            papers = data["data"]
            # resort based on influentialCitationCount - is this good?
            papers.sort(key=lambda x: x["influentialCitationCount"], reverse=True)
            logger.info(
                f"Found {data['total']} papers, analyzing {_offset} to {_offset + len(papers)}"
            )

            async def process_paper(paper, i):
                path = os.path.join(pdir, f'{paper["paperId"]}.pdf')
                success = await scraper.scrape(paper, path, i=i, logger=logger)
                if success:
                    bibtex = paper["citationStyles"]["bibtex"]
                    key = bibtex.split("{")[1].split(",")[0]
                    return path, dict(
                        citation=format_bibtex(bibtex, key),
                        key=key,
                        bibtex=bibtex,
                        tldr=paper["tldr"],
                        year=paper["year"],
                        url=paper["url"],
                        paperId=paper["paperId"],
                    )
                return None, None

            # batch them, since since we may reach desired limit before all done
            for i in range(0, len(papers), batch_size):
                batch = papers[i : i + batch_size]
                results = await asyncio.gather(
                    *[process_paper(p, i + j) for j, p in enumerate(batch)]
                )
                for path, info in results:
                    if path is not None:
                        paths[path] = info
                # if we have enough, stop
                if len(paths) >= limit:
                    break
    if len(paths) < limit and _offset + _limit < data["total"]:
        paths.update(
            await a_search_papers(
                query,
                limit=limit,
                pdir=pdir,
                _paths=paths,
                _limit=_limit,
                _offset=_offset + _limit,
                logger=logger,
                year=year,
                verbose=verbose,
                scraper=scraper,
                batch_size=batch_size,
            )
        )
    if _offset == 0:
        await scraper.close()
    return paths


def search_papers(
    query,
    limit=10,
    pdir=os.curdir,
    semantic_scholar_api_key=None,
    _paths=None,
    _limit=100,
    _offset=0,
    logger=None,
    year=None,
    verbose=False,
    scraper=None,
    batch_size=10,
):
    # special case for jupyter notebooks
    if "get_ipython" in globals() or "google.colab" in sys.modules:
        import nest_asyncio

        nest_asyncio.apply()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as e:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(
        a_search_papers(
            query,
            limit=limit,
            pdir=pdir,
            semantic_scholar_api_key=semantic_scholar_api_key,
            _paths=_paths,
            _limit=_limit,
            _offset=_offset,
            logger=logger,
            year=year,
            verbose=verbose,
            scraper=scraper,
            batch_size=batch_size,
        )
    )
