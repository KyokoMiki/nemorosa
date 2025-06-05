#!/usr/bin/env python

# imports: standard
import logging
import pickle

# constants
COOKIE_FILE = 'cookies.dat'


def get_cookies():
    cookies = None
    logging.info("Attempting to load previous cookies")
    # attempt to load previous cookies
    try:
        cookies = pickle.load(open(COOKIE_FILE, 'rb'))
    except Exception as e:
        logging.info("Previous cookies not loaded. %s", e)
    else:
        logging.info("Cookies loaded from %s", COOKIE_FILE)
    # return cookies
    return cookies


def save_cookies(cookies):
    logging.info("Saving cookies to %s", COOKIE_FILE)
    pickle.dump(cookies, open(COOKIE_FILE, 'wb'))

    