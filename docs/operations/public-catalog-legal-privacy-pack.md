# PRO-STOR public catalog: legal and privacy launch pack

For the owner and the lawyer. It lists what needs external approval before
the public request form goes live on `pro-stor.ru`, and what the code
already records. It contains no legal wording: every text below that a
customer would read must come from the lawyer.

## What the public site collects

| Where | Data | Stored in | Who can read it |
| --- | --- | --- | --- |
| Request form | name, phone, preferred messenger (Telegram or MAX), optional comment | `customer_requests_customerrequest` | staff with the sales right, in DenisStock "Заявки клиентов"; never the public process (column grants) |
| Request form | the cart lines: part, quantity, unit, price at sending, supply-inquiry flag | `customer_requests_customerrequestline` | same |
| Request form | consent evidence: privacy policy version, consent version, purpose `public_request_contact`, acceptance time | request row | same |
| Telegram link (after an operator issues it and the customer starts the bot) | Telegram chat id | `customer_requests_customerrequestmessengercontact` | same |
| Browser | `prostor_cart` (signed cart and form token), `prostor_csrf` | the customer's browser only | the customer |

No analytics, advertising or third-party scripts are loaded (the content
security policy forbids scripts). No account, password or payment data
exists. Client addresses are used only as a hashed key of a short-lived
in-memory counter (the request rate limit) and are not stored.

## Needs approval before launch (external)

1. **Privacy policy.** Text, the operator's details, and a public URL. The
   site has no policy page yet; the form must link to it once it exists
   (a template change in `templates/public_catalog/request_form.html`).
2. **Personal-data consent.** The checkbox text is a placeholder:
   "Согласен на обработку персональных данных для связи по этой заявке."
   The lawyer supplies the final text and decides whether it may stay a
   checkbox next to the form.
3. **Version identifiers.** Each approved text gets an identifier set in
   `.env.public`: `PUBLIC_REQUEST_PRIVACY_POLICY_VERSION`,
   `PUBLIC_REQUEST_PERSONAL_DATA_CONSENT_VERSION`. They are stored on every
   request at sending and never change afterwards. Today both default to
   `draft-legal-review-1`, so every request sent before approval is
   recognisably pre-approval.
4. **Retention.** How long request contacts are kept after the request is
   completed or cancelled, and who deletes them. The code anonymises on
   demand (below); an automatic retention job does not exist and would be
   a new feature once the period is decided.
5. **Messenger disclosure.** Whether the policy must name Telegram and MAX
   as channels (the site records only the chat id, after the customer
   starts the bot from a link the operator sends).
6. **Operator obligations under 152-FZ**, for the owner and the lawyer:
   notification of the regulator, and where the database physically is
   (the VPS location) with respect to data localisation.
7. **Cookies.** The two cookies are strictly functional (cart and CSRF
   protection). Whether any notice is still wanted is the lawyer's call.

## What the code already supports

* Versioned consent with the purpose and timestamp on every request
  (`apps/customer_requests/models.py`, `policies.py`).
* The form does not submit without the consent checkbox; the checkbox is
  then marked invalid and linked to the reason.
* Consent withdrawal and irreversible anonymisation of name, phone and
  comment, each with an audit event (`withdraw_consent`, `anonymize_request`
  in `apps/customer_requests/services.py`). There is no screen for them
  yet: an administrator runs them from `manage.py shell` with the request
  id. A small operator action is the natural follow-up once retention is
  decided.
* The public database role cannot read any earlier request's personal
  data (proved on PostgreSQL 16, see
  `docs/qualification/public-catalog-final-night-review.md`).

## Launch gate

The request form may be shown on the preview with the draft identifiers.
Before `pro-stor.ru` accepts real requests: items 1, 2, 3 and 6 done, the
policy linked from the form, and the version identifiers set to the
approved texts.
