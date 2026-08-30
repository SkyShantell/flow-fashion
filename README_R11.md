# Flow Try-On Factory R11

Built on the working R10.1 Drive archive baseline.

## Google Sheets readability improvements
- Frozen header row and first two columns
- Bold high-contrast header styling
- Filter controls on the header row
- Practical column widths and wrapped product names
- Product/Drive URLs displayed as short clickable labels
- Technical metadata is preserved but hidden by default
- Visible columns prioritize product, focus, statuses, approval, permanent Drive media, and archive folder
- Formatting is applied on both Replace tab and Append rows pushes
- If Google rejects a visual formatting request, the data push still succeeds and reports a formatting warning

The CSV export remains unchanged and continues to include raw URLs/IDs.
