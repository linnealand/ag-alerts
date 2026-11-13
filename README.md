# ag-alerts
Push notifications for American Girl sales.

A GitHub Actions workflow runs hourly to check the website's sale feeds and sends a push notification through [ntfy](https://ntfy.sh) when an in-stock item newly reaches 60% off or more.

Looks only at products in the Last Chance Outlet. Skips anything tagged *Girls Clothing and Accessories* or *Adult Clothing and Accessories*.

Run locally without sending anything:

```bash
python3 check_sales.py --dry-run
```

Test ntfy notifications:

```bash
python3 check_sales.py --test-notification
```
