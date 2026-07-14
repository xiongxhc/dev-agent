"""M25 design-diff unit tests — pure, no docker/tokens."""
from devagent.design_diff import diff_designs, load_design
from devagent.schema import Contract, ServiceNode, SystemDesign


def _design(api_slice="a JSON API", web_slice="a UI", api_paths=None, tables=None,
            web_name="web", extra_services=()):
    api_paths = api_paths or {"/api/todos": {"get": {}}}
    tables = tables or {"todos": ["id", "title"]}
    return SystemDesign(
        title="Todos",
        services=[
            ServiceNode(id="db", name="db", kind="datastore", stack="postgres",
                        prd_slice="store", provides=["db.schema"]),
            ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                        prd_slice=api_slice, depends_on=["db"], provides=["api.openapi"],
                        consumes=["db.schema"]),
            ServiceNode(id="web", name=web_name, kind="frontend", stack="node-vite-react",
                        prd_slice=web_slice, depends_on=["api"], consumes=["api.openapi"]),
            *extra_services,
        ],
        contracts=[
            Contract(id="db.schema", kind="db_schema", producer="db", spec={"tables": tables}),
            Contract(id="api.openapi", kind="openapi", producer="api",
                     spec={"paths": api_paths}),
        ])


def test_identical_designs_change_nothing():
    d = diff_designs(_design(), _design())
    assert d.changed_ids == [] and not d.schema_changed and d.renamed == []


def test_prd_slice_change_rebuilds_only_that_service():
    d = diff_designs(_design(), _design(web_slice="a UI with dark mode"))
    assert d.changed_ids == ["web"] and not d.schema_changed


def test_contract_change_rebuilds_producer_and_consumers_in_topo_order():
    d = diff_designs(_design(),
                     _design(api_paths={"/api/todos": {"get": {}, "post": {}}}))
    assert d.changed_ids == ["api", "web"]      # producer, then consumer (topo order)
    assert not d.schema_changed


def test_db_schema_change_sets_schema_changed():
    d = diff_designs(_design(), _design(tables={"todos": ["id", "title", "done"]}))
    assert d.schema_changed
    assert "db" in d.changed_ids and "api" in d.changed_ids   # provider + consumer


def test_new_service_is_changed():
    extra = ServiceNode(id="jobs", name="jobs", kind="backend", stack="node-express",
                        prd_slice="background jobs")
    d = diff_designs(_design(), _design(extra_services=(extra,)))
    assert d.changed_ids == ["jobs"] and not d.schema_changed


def test_rename_is_flagged_and_treated_as_changed():
    d = diff_designs(_design(), _design(web_name="webapp"))
    assert d.renamed == ["web"] and "web" in d.changed_ids


def test_removed_db_schema_contract_resets_data():
    new = SystemDesign(
        title="Todos",
        services=[ServiceNode(id="api", name="api", kind="backend", stack="node-express",
                              prd_slice="a JSON API", provides=["api.openapi"])],
        contracts=[Contract(id="api.openapi", kind="openapi", producer="api",
                            spec={"paths": {"/api/todos": {"get": {}}}})])
    assert diff_designs(_design(), new).schema_changed


def test_db_schema_contract_flipping_kind_resets_data():
    """Same contract id repurposed to a different kind = the schema is gone: reset."""
    prior = _design()
    new = _design()
    flipped = [c.model_copy(update={"kind": "event"}) if c.id == "db.schema" else c
               for c in new.contracts]
    new = new.model_copy(update={"contracts": flipped})
    assert diff_designs(prior, new).schema_changed


def test_load_design_roundtrip(tmp_path):
    d = _design()
    (tmp_path / "design.json").write_text(d.model_dump_json(indent=2))
    assert load_design(tmp_path) == d
