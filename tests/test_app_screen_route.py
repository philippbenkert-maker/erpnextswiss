from erpnextswiss import hooks


def test_apps_screen_opens_the_internal_workspace():
    assert hooks.app_home == "/desk/schweizer-buchhaltung"
    assert hooks.add_to_apps_screen[0]["route"] == hooks.app_home


def test_legacy_public_routes_redirect_to_the_workspace():
    assert hooks.website_redirects == [
        {
            "source": "/schweizer-buchhaltung",
            "target": hooks.app_home,
            "forward_query_parameters": True,
        },
        {
            "source": "/erpnextswiss",
            "target": hooks.app_home,
            "forward_query_parameters": True,
        },
    ]
