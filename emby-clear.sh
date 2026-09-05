#!/bin/bash

EMBY_HOST="http://192.168.2.200:8096"
API_KEY="2fdeadd813c04c6eae1497d0f75dfa0e"

# Usage: ./emby-clear.sh [logos|numbers|refresh|all]
ACTION="${1:-all}"

if [[ "$ACTION" == "refresh" || "$ACTION" == "all" ]]; then
  # Refresh Live TV tuners
  echo "Refreshing Live TV tuners..."
  curl -s -X POST "${EMBY_HOST}/emby/LiveTv/TunerHosts/Discover?api_key=${API_KEY}" > /dev/null
  echo "Tuners refreshed."

  # Trigger guide refresh task
  echo "Triggering guide refresh..."
  TASK_ID=$(curl -s "${EMBY_HOST}/emby/ScheduledTasks?api_key=${API_KEY}" \
    | python3 -c "import sys,json; tasks=[t for t in json.load(sys.stdin) if 'guide' in t['Name'].lower()]; print(tasks[0]['Id']) if tasks else None")
  curl -s -X POST "${EMBY_HOST}/emby/ScheduledTasks/Running/${TASK_ID}?api_key=${API_KEY}" > /dev/null
  
  echo "Guide refresh triggered. Waiting for completion..."
  sleep 3
  while true; do
    STATE=$(curl -s "${EMBY_HOST}/emby/ScheduledTasks/${TASK_ID}?api_key=${API_KEY}" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print('%s|%s' % (d.get('State',''), d.get('CurrentProgressPercentage') or 0))")
    if [[ "${STATE%%|*}" == "Idle" ]]; then
      break
    fi
    echo "  Guide refresh: ${STATE%%|*} ${STATE##*|}%"
    sleep 5
  done
  echo "Guide refresh complete."
fi

#if [[ "$ACTION" == "logos" || "$ACTION" == "all" ]]; then
if [[ "$ACTION" == "logos" ]]; then
  echo "Fetching item IDs for logo clearing..."
  ITEM_IDS=$(curl -s "${EMBY_HOST}/emby/Items?Recursive=true&api_key=${API_KEY}" \
    | python3 -c "import sys,json; [print(i['Id']) for i in json.load(sys.stdin)['Items']]")
  TOTAL=$(echo "$ITEM_IDS" | wc -l)
  echo "Found ${TOTAL} items. Clearing logos..."
  echo "$ITEM_IDS" | xargs -P 32 -I{} bash -c \
    "curl -s -X DELETE \"${EMBY_HOST}/emby/Items/{}/Images/Logo?api_key=${API_KEY}\" > /dev/null && echo \"  Cleared logo: {}\""
  echo "Logos cleared."

fi

if [[ "$ACTION" == "numbers" || "$ACTION" == "all" ]]; then
  echo "Fetching Live TV channel IDs for number clearing..."
  CHANNEL_IDS=$(curl -s "${EMBY_HOST}/emby/LiveTv/Channels?Limit=99999&api_key=${API_KEY}" \
    | python3 -c "import sys,json; [print(c['Id']) for c in json.load(sys.stdin)['Items']]")
  TOTAL=$(echo "$CHANNEL_IDS" | wc -l)
  echo "Found ${TOTAL} channels. Clearing channel numbers..."
  echo "$CHANNEL_IDS" | while read -r ID; do
    ITEM=$(curl -s "${EMBY_HOST}/emby/LiveTv/Channels/${ID}?api_key=${API_KEY}")
    PATCHED=$(echo "$ITEM" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['ChannelNumber'] = ''
d['Number'] = ''
d['LockedFields'] = [f for f in d.get('LockedFields', []) if f not in ('ChannelNumber', 'SortName')]
print(json.dumps(d))
")
    curl -s -X POST "${EMBY_HOST}/emby/Items/${ID}?api_key=${API_KEY}" \
      -H "Content-Type: application/json" \
      -d "$PATCHED" > /dev/null
    echo "  Cleared number: ${ID}"
  done
  echo "Channel numbers cleared."
fi

echo "Done."
