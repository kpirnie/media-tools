#!/usr/bin/env bash

extra_epgs=(
	"https://epg.kptv.im/7daygracenote/USFast.xml"
	"https://epg.kptv.im/7daygracenote/Canada.xml"
	"https://lv-shark.com/xmltv.php?username=29797330&password=53168726"
	"https://six.kptv.im/xmltv.php?username=1AlwoFxJ1QMeV5lP&password=wdEeiUp5LAHHXSnr"
	"https://prada.kptv.im/xmltv.php?username=6789278523&password=6238432868"
	"https://kwick.kptv.im/xmltv.php?username=kpirnie&password=ztVSVD7H7rhRPGha"
	"https://argon.kptv.im/xmltv.php?username=gmarie&password=176324745544"
	"https://v12.kptv.im/xmltv.php?username=G8NAFVO9&password=96421197"
	"https://crx.watch/xmltv.php?username=kpirnietc&password=721499408818"
	"http://demon-cable.xyz/xmltv.php?username=D2KPIRNIE&password=52529246"
	"https://demon.ghostcable-epg.fyi"
)

# input dir for the sync
_input="/mnt/web/pdn/sites/sites/kptv/html/my_epg/epgs"

# build the --extra-url arguments
_extra_args=()
for _url in "${extra_epgs[@]}"; do
	_extra_args+=( --extra-url "${_url}" )
done

/usr/local/bin/epg-sync \
	--user-agent "NEUTRO_PRO/windows_msix" \
	--input "${_input}" \
	"${_extra_args[@]}" \
	--output /mnt/web/pdn/sites/sites/kptv/html/my_epg/epg.xml
