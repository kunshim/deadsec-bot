from playwright.sync_api import sync_playwright
import sys
import time

def login(uid, pwd):
    page.click('xpath=//html/body/div[1]/div/div/div[1]/div/nav/ul/div[1]/button')
    page.fill('xpath=//html/body/div[4]/div/div/div[2]/form/div[1]/div/input', uid)
    page.fill('xpath=//html/body/div[4]/div/div/div[2]/form/div[2]/div/input', pwd)
    page.click('xpath=//html/body/div[4]/div/div/div[2]/form/div[3]/div/button[1]')
    print('[+] login success!')

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    uid = 'deadsec@osori.site'
    password = '358X^VJKz{4}'
    context = browser.new_context()
    context.add_cookies([{
        'name': 'connect.sid',
        'value': 's%3AVG3DvIrKyOSQ7Zlk_b4Br1FyYJNJvgvn.rEp%2BUo42O0kigYcnKi%2FagNeH6DZkWxp0QLnSdxJ3MK8',
        'domain': 'ctfmd.osori.site',
        'secure': True,
        'expires': 1763588029,
        'path': '/'
        }])
    page = context.new_page()
    page.goto('https://ctfmd.osori.site')
    #login(uid, password)
    cmd = input()
    if cmd == 'new':
        chall_name = input()
        ctf_name = input()
        content = f'---\ntags : {ctf_name}\n---\n# {chall_name}'
        page.click('xpath=//html/body/div[1]/div/div/div[1]/div/nav/ul/div[2]/a')
        time.sleep(3)
        page.keyboard.type(content)
        page.click('#permissionLabelEditable')
        page.click('xpath=//html/body/div[3]/div[2]/div[1]/small/span[2]/ul/li[1]/a')
        print(page.url + '?both')
    browser.close()