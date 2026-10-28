# Public marketing site launch checklist

## One-time content rollout

After deploying code and running migrations, run `python manage.py bootstrap_marketing --refresh --configure-domain` **once**. It imports the tracked media into Wagtail, updates the four public marketing pages and home page, creates `/resources/` with three guides, and sets the default Wagtail hostname to `MARKETING_SITE_DOMAIN`. Subsequent runs without `--refresh` only import missing assets and create missing guides; they do not overwrite editor changes. Do not re-run `--refresh` after editors have updated copy or images.

Set `MARKETING_SITE_DOMAIN=www.novenaplatform.com` and `APP_DOMAIN=app.novenaplatform.com` in production. Configure the public web server to redirect `novenaplatform.com` to `https://www.novenaplatform.com`, and route both public and app hosts to Django. Verify HTTPS, `ALLOWED_HOSTS`, and login/signup links on the production build. The marketing page canonical URLs point to `www`; Wagtail serves sitemap entries for the live pages. `robots.txt` blocks authenticated routes and the draft legal pages.

## Review gates before public launch

- Legal counsel must approve Terms, Privacy, legal entity and contact details, retention schedule, processor and transfer disclosures, and final paid plan language. Draft legal pages remain `noindex` and labelled as drafts. Remove the draft banners and indexing blocks only after approval.
- Public marketing analytics and inquiry event tracking are disabled in the marketing base template. Obtain legal approval for measurement and update the privacy notice and consent controls before enabling them.
- Confirm the indicative S$99/S$299 platform prices and final commercial scope. Hardware, installation, connectivity, and integrations remain separately quoted.
- Review the five synthetic-data Hub screenshots after any major UI change. The source stock photography is recorded in `static/marketing/ASSET_SOURCES.md`.
- Confirm all images resolve from production media storage. Wagtail renditions are generated on demand from the imported originals.
- Check desktop/mobile navigation, all solution anchors, Resources guides, inquiry success and error states, `/privacy/`, `/terms/`, `/sitemap.xml`, `/robots.txt`, and the API reference link.
- Run `npm run build`, `python manage.py check`, `python manage.py migrate --check`, targeted tests, and `python manage.py production_readiness_check` with production env.

## Local browser evidence

The reviewed desktop first viewport captures are in `docs/marketing_screenshots/`: `home-desktop.jpg`, `platform-desktop.jpg`, `solutions-desktop.jpg`, `pricing-desktop.jpg`, `resources-desktop.jpg`, and `about-desktop.jpg`. The 390px mobile captures are `home-mobile.jpg` and `solutions-mobile.jpg`. They were taken from the local Wagtail site with the development toolbar disabled. Before launch, repeat this visual review against the production build and record a mobile check for each page and the three guides.
