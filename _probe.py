import re
text = open('src/components/TelemetryChart.tsx').read()
# Find the full useTelemetry body (returns an object)
m = re.search(r'export function useTelemetry\([^)]*\)[^{]*', text)
if not m:
    print('NOT FOUND: useTelemetry declaration'); sys.exit(0)
print('DECL:', m.group(0))
start = m.end()
# Parse brace depth to find the matching close
depth = 0
i = start
while i < len(text):
    if text[i] == '{': depth += 1
    elif text[i] == '}':
        depth -= 1
        if depth == 0:
            break
    i += 1
body = text[start:i]
print(f'\n=== useTelemetry body ({len(body)} chars) ===')
print(body[:8000])