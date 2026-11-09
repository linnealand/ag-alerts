# ag-alerts
Push notifications for American Girl sales.

A GitHub Actions workflow runs hourly to check the store's sale feeds and sends a push notification through [ntfy](https://ntfy.sh) when an in-stock item newly reaches 60% off or more.

Looks at both the `sale` and `american-girl-last-chance` collections. Skips anything tagged *Girls Clothing and Accessories* or *Adult Clothing and Accessories*.

Run locally without sending anything:

```bash
python3 check_sales.py --dry-run
```

Test ntfy notifications:

```bash
python3 check_sales.py --test-notification
```
