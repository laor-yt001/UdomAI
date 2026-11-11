import os

cookies = """# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
# This is a generated file! Do not edit.

.youtube.com	TRUE	/	TRUE	1785073090	GPS	1
.youtube.com	TRUE	/	TRUE	1819631395	PREF	tz=America.Los_Angeles
.youtube.com	TRUE	/	FALSE	1819631384	HSID	A8SslE4_oGt9cdsh2
.youtube.com	TRUE	/	TRUE	1819631384	SSID	AE2gqknmMzY29A3h8
.youtube.com	TRUE	/	FALSE	1819631384	APISID	7bwnlhCCri-h0FDy/A0Wmgrm4b6QHqYemn
.youtube.com	TRUE	/	TRUE	1819631384	SAPISID	92Xf-tD5m-OR0lBd/AY_q1e5SoambZ5VIA
.youtube.com	TRUE	/	TRUE	1819631384	__Secure-1PAPISID	92Xf-tD5m-OR0lBd/AY_q1e5SoambZ5VIA
.youtube.com	TRUE	/	TRUE	1819631384	__Secure-3PAPISID	92Xf-tD5m-OR0lBd/AY_q1e5SoambZ5VIA
.youtube.com	TRUE	/	FALSE	1819631386	SID	g.a000Awk_7L5RvwFKdvwSrq6nzl6jUqBE76A0T__tKjWyyTw4ajgN0lqxfe3C_61B7EJd3kw4RgACgYKAWsSARASFQHGX2MiAVhfmx_gJr4JYaDr9JBMPxoVAUF8yKpHRe5wYHMktfRkBLjy-_Lo0076
.youtube.com	TRUE	/	TRUE	1819631386	__Secure-1PSID	g.a000Awk_7L5RvwFKdvwSrq6nzl6jUqBE76A0T__tKjWyyTw4ajgNyYDQ2iHPAZgHqrh7CGU9wAACgYKAcQSARASFQHGX2MiH92wOWYIiSmRjwk9YSG1ABoVAUF8yKpmewm2AnikrD9sDYq86SiS0076
.youtube.com	TRUE	/	TRUE	1819631386	__Secure-3PSID	g.a000Awk_7L5RvwFKdvwSrq6nzl6jUqBE76A0T__tKjWyyTw4ajgNkFDxXmEFIqQyyDVvsSKSmAACgYKAaYSARASFQHGX2MiKWiqflDDr7BHndBBM8xTShoVAUF8yKpT1R1_4pdabyMGKIac61i60076
.youtube.com	TRUE	/	TRUE	1819631387	LOGIN_INFO	AFmmF2swRAIgaD70ip7kgF-0-_Ju4ST_rJe1DCqTiDpqf3c-WzFViP4CIA6uXCR2-aLlsG18aQX4LHzubVZqhN8EtS2rQLgvik6w:QUQ3MjNmd3BlTW91aWpRc2lkQkRqT1MxYmlUZlZocW9hbU41dUxIUmhLdFh0NHRucWViSjdyWnlDS2txTWZUdG1Vb1QyR2RrVlNzT0pzd1dTaHBzQkZEMGdaZ3BDMUlnblRaNmFySmRYYUhjN2tjUHB4VDNpQWdjYzMyZ3YtY0xSX2tsVVlnVGtacmthMThiWG1pOEVKZGw2OGlUUm9IMGFn
.youtube.com	TRUE	/	TRUE	1816607889	__Secure-1PSIDTS	sidts-CjcBPWEu2QxzgZ8rPDjBm6yIBZy_r20QuAFCW30MML1_LLoSAz_EXzkmX6T1MYZTnfHC5jM1F315EAA
.youtube.com	TRUE	/	TRUE	1785072489	__Secure-1PSIDRTS	sidts-CjcBPWEu2QxzgZ8rPDjBm6yIBZy_r20QuAFCW30MML1_LLoSAz_EXzkmX6T1MYZTnfHC5jM1F315EAA
.youtube.com	TRUE	/	TRUE	1816607889	__Secure-3PSIDTS	sidts-CjcBPWEu2QxzgZ8rPDjBm6yIBZy_r20QuAFCW30MML1_LLoSAz_EXzkmX6T1MYZTnfHC5jM1F315EAA
.youtube.com	TRUE	/	TRUE	1785072489	__Secure-3PSIDRTS	sidts-CjcBPWEu2QxzgZ8rPDjBm6yIBZy_r20QuAFCW30MML1_LLoSAz_EXzkmX6T1MYZTnfHC5jM1F315EAA
.youtube.com	TRUE	/	TRUE	1785072493	CONSISTENCY	AHDYFaELFaf4ruWGXyFM3DflkVjrD5YULekboI27-ch_UDCXyXn7ai5bkpWq5guEu0z8Jj9o5-kbW0j5GNoujDljaMjfEYZkuatFWMgfQzBKXaltEN2KKNdzht2C2_kdzI0fCSd6FAqy0V81Lze782kV
.youtube.com	TRUE	/	FALSE	1816608165	SIDCC	AKEyXzXunkgAZOfHqz69e3SfReYmHr_ssUTOoJauGPjH7o47-1dyAK1eIWukbuTzirnswekm
.youtube.com	TRUE	/	TRUE	1816608165	__Secure-1PSIDCC	AKEyXzVSIEuGyhl2VFKxnYj1aXOXSEg9_5YSarpsqgnQsX06VLXlPzQbnfOcFUUQbhlMoJly
.youtube.com	TRUE	/	TRUE	1816608165	__Secure-3PSIDCC	AKEyXzWClz0g_Sh5qUWHW9Y-HOiIOx2vglcxNGLy1S2aWq-wfNMW5dOem6eEZuOS97Gl1VcC
.youtube.com	TRUE	/	TRUE	0	YSC	W5OKBuFyr9Q
.youtube.com	TRUE	/	TRUE	1800623410	VISITOR_INFO1_LIVE	r3NNowPn8Ow
.youtube.com	TRUE	/	TRUE	1800623410	VISITOR_PRIVACY_METADATA	CgJVUxIEGgAgGA%3D%3D
.youtube.com	TRUE	/	TRUE	1800623290	__Secure-YNID	20.YT=fvr97s62vzr7-K0hYjjiwAItZdBjDakrpjRcBnQ7XXP4G73-U4bIUbuxPn2PCs4uTabwiJWeVpO9XfrtGlLvfebFYUIB7DMv8_y_EpsisSC5ib_KHnB2P1kd1ZezbOD1C2YVLce-OQo4v4wZFB9n-LGZvD4PjA61O3oGaZYLGs_WFzMiqbYPbhC4QiQofEwhB_1yEKrYkBqBm6XLrnKzwdc8jSZANtkaGHH8jDivsp7zKhGa7M_lXm5AeMUDsSNmk_YuQUtx6dy-EPWeCmEwFv-vM6Oc5qUQVQdjYGmD1wtFfueaZjnhTRFseuw3KXk6-XdukCl9YotCSIlQKcKFIA
.youtube.com	TRUE	/	TRUE	1800623298	__Secure-ROLLOUT_TOKEN	CLKMw9651dHS9wEQwNe24LTwlQMY5Y-c5LTwlQM%3D"""

formatted_cookies = '"' + cookies.replace('\\n', '\\\\n').replace('\n', '\\n') + '"'

with open('.env', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, l in enumerate(lines):
    if l.startswith('WWW.YOUTUBE.COM_COOKIES.TXT'):
        lines[i] = f'WWW.YOUTUBE.COM_COOKIES.TXT={formatted_cookies}\n'
        break

with open('.env', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print('Updated .env with fresh cookies!')
