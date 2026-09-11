# Public catalog read/cart hardening

The public runtime accepts search text only through Search 2.0's 64-character
normalizer. Pagination is bounded by Search 2.0 to 50 records per page and
300 ranked records total. The anonymous cart uses a signed cookie, is capped
at 50 lines and 1,000 units per line, ignores any client price, and rechecks
current public availability before adding a line. It never writes stock or a
reservation.

The public settings bound request bodies to 64 KiB and form fields to 32,
enable HttpOnly/Lax cookies, retain Django CSRF protection for cart writes and
mark cart responses `never_cache`. Public routes remain GET-only except the
two explicit CSRF-protected cart mutations.
