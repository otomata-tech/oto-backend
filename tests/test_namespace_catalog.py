"""Le catalogue de namespaces des instructions serveur est DÉRIVÉ du registre
(`providers.render_namespace_catalog`) → garde-fou anti-dérive : tout namespace
déclaré au registre (hors transports email pur-credential) doit apparaître, et les
concepts spine doivent être présents. Casse si on ajoute un connecteur sans qu'il
soit présenté — exactement le bug qu'on corrige (reddit/culture cités, apollo/
foncier/pennylane omis dans la liste écrite à la main).
"""
from oto_mcp import providers


def test_every_registry_namespace_is_presented():
    cat = providers.render_namespace_catalog()
    for c in providers._REGISTRY_LIST:
        if c.name in providers.EMAIL_CONNECTOR_TRANSPORT:
            continue  # credential-only → présenté via le concept spine email_send
        for ns in c.namespaces:
            assert f"{ns}_*" in cat, f"namespace {ns}_* absent du catalogue (connecteur {c.name})"


def test_email_transports_not_listed_as_namespaces():
    cat = providers.render_namespace_catalog()
    # scaleway/resend = pur credential (aucun tool propre) → pas une ligne de namespace
    assert "scaleway_*" not in cat
    assert "resend_*" not in cat
    # …mais l'email reste présenté comme concept spine
    assert "email_send" in cat


def test_spine_concepts_present():
    cat = providers.render_namespace_catalog()
    for ns, _ in providers.SPINE_CONCEPTS:
        head = ns.split(" ")[0]   # "run_* / feedback" → "run_*"
        assert head in cat, f"concept spine {head} absent"
    # les piliers concrets
    for token in ("data_*", "oto_*"):
        assert token in cat


def test_availability_annotations():
    cat = providers.render_namespace_catalog()
    # un opt-in gaté est annoté (ne pas faire croire qu'il est appelable d'office)
    assert "apollo_* — Apollo.io" in cat
    line = next(l for l in cat.splitlines() if l.startswith("• apollo_*"))
    assert "à activer" in line
    # un connecteur du bundle par défaut n'a pas l'annotation « à activer »
    serper = next(l for l in cat.splitlines() if l.startswith("• serper_*"))
    assert "à activer" not in serper


def test_injected_into_server_instructions():
    from oto_mcp import server
    assert "data_*" in server._SERVER_INSTRUCTIONS
    assert "apollo_*" in server._SERVER_INSTRUCTIONS
    # plus de double mention « (MCP fédéré) (MCP fédéré) »
    assert "(MCP fédéré) (MCP fédéré)" not in server._SERVER_INSTRUCTIONS
