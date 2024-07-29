import sempy.fabric as fabric
import pandas as pd
import warnings
import datetime
from IPython.display import display, HTML
from pyspark.sql import SparkSession
from sempy_labs._model_dependencies import get_model_calc_dependencies
from sempy_labs._helper_functions import (
    format_dax_object_name,
    resolve_lakehouse_name,
    create_relationship_name,
)
from sempy_labs.lakehouse._get_lakehouse_tables import get_lakehouse_tables
from sempy_labs.lakehouse._lakehouse import lakehouse_attached
from sempy_labs.tom import connect_semantic_model
from sempy_labs._model_bpa_rules import model_bpa_rules
from typing import Optional
from sempy._utils._log import log
import sempy_labs._icons as icons
from synapse.ml.services import Translate
from pyspark.sql.functions import col, flatten
from pyspark.sql.types import StructType, StructField, StringType
import polib
import os


@log
def run_model_bpa(
    dataset: str,
    rules: Optional[pd.DataFrame] = None,
    workspace: Optional[str] = None,
    export: Optional[bool] = False,
    return_dataframe: Optional[bool] = False,
    extended: Optional[bool] = False,
    language: Optional[str] = None,
    **kwargs,
):
    """
    Displays an HTML visualization of the results of the Best Practice Analyzer scan for a semantic model.

    Parameters
    ----------
    dataset : str
        Name of the semantic model.
    rules : pandas.DataFrame, default=None
        A pandas dataframe containing rules to be evaluated.
    workspace : str, default=None
        The Fabric workspace name.
        Defaults to None which resolves to the workspace of the attached lakehouse
        or if no lakehouse attached, resolves to the workspace of the notebook.
    export : bool, default=False
        If True, exports the resulting dataframe to a delta table in the lakehouse attached to the notebook.
    return_dataframe : bool, default=False
        If True, returns a pandas dataframe instead of the visualization.
    extended : bool, default=False
        If True, runs the set_vertipaq_annotations function to collect Vertipaq Analyzer statistics to be used in the analysis of the semantic model.
    language : str, default=None
        Specifying a language code (i.e. 'it-IT' for Italian) will auto-translate the Category, Rule Name and Description into the specified language.
        Defaults to None which resolves to English.

    Returns
    -------
    pandas.DataFrame
        A pandas dataframe in HTML format showing semantic model objects which violated the best practice analyzer rules.
    """

    if "extend" in kwargs:
        print(
            "The 'extend' parameter has been deprecated. Please remove this parameter from the function going forward."
        )
        del kwargs["extend"]

    warnings.filterwarnings(
        "ignore",
        message="This pattern is interpreted as a regular expression, and has match groups.",
    )
    warnings.filterwarnings(
        "ignore", category=UserWarning, message=".*Arrow optimization.*"
    )

    language_list = [
        "it-IT",
        "es-ES",
        "he-IL",
        "pt-PT",
        "zh-CN",
        "fr-FR",
        "da-DK",
        "cs-CZ",
        "de-DE",
        "el-GR",
        "fa-IR",
        "ga-IE",
        "hi-IN",
        "hu-HU",
        "is-IS",
        "ja-JP",
        "nl-NL",
        "pl-PL",
        "pt-BR",
        "ru-RU",
        "te-IN",
        "ta-IN",
        "th-TH",
        "zu-ZA",
        "am-ET",
        "ar-AE",
    ]

    workspace = fabric.resolve_workspace_name(workspace)

    if language is not None and language not in language_list:
        print(f"{icons.yellow_dot} The '{language}' is not in our predefined language list. Please file an issue and let us know which language you are using: `<https://github.com/microsoft/semantic-link-labs/issues/new?assignees=&labels=&projects=&template=bug_report.md&title=>`_.")

    if extended:
        with connect_semantic_model(
            dataset=dataset, workspace=workspace, readonly=False
        ) as tom:
            tom.set_vertipaq_annotations()

    with connect_semantic_model(
        dataset=dataset, workspace=workspace, readonly=True
    ) as tom:

        dep = get_model_calc_dependencies(dataset=dataset, workspace=workspace)

        # Translations
        if language is not None and rules is None and language in language_list:
            # Use the polib to get the translations without using spark
            rules = model_bpa_rules(
                dataset=dataset, workspace=workspace, dependencies=dep
            )
            current_dir = os.path.dirname(os.path.abspath(__file__))
            for c in ["Category", "Description", "Rule Name"]:
                po = polib.pofile(
                    f"{current_dir}/_bpa_translation/_translations_{c.lower().replace(' ','')}.po"
                )
                for entry in po:
                    if entry.tcomment == language:
                        rules.loc[rules["Rule Name"] == entry.msgid, c] = entry.msgstr
        elif language is not None and (rules is not None or language not in language_list):
            rules = model_bpa_rules(dataset=dataset, workspace=workspace, dependencies=dep)
            # Use spark to get the translations
            rules_temp = rules.copy()
            rules_temp = rules_temp.drop(["Expression", "URL", "Severity"], axis=1)

            schema = StructType(
                [
                    StructField("Category", StringType(), True),
                    StructField("Scope", StringType(), True),
                    StructField("Rule Name", StringType(), True),
                    StructField("Description", StringType(), True),
                ]
            )

            spark = SparkSession.builder.getOrCreate()
            dfRules = spark.createDataFrame(rules_temp, schema)

            columns = ["Category", "Rule Name", "Description"]
            for clm in columns:
                translate = (
                    Translate()
                    .setTextCol(clm)
                    .setToLanguage(language)
                    .setOutputCol("translation")
                    .setConcurrency(5)
                )

                if clm == "Rule Name":
                    transDF = (
                        translate.transform(dfRules)
                        .withColumn(
                            "translation", flatten(col("translation.translations"))
                        )
                        .withColumn("translation", col("translation.text"))
                        .select(clm, "translation")
                    )
                else:
                    transDF = (
                        translate.transform(dfRules)
                        .withColumn(
                            "translation", flatten(col("translation.translations"))
                        )
                        .withColumn("translation", col("translation.text"))
                        .select("Rule Name", clm, "translation")
                    )

                df_panda = transDF.toPandas()
                rules = pd.merge(
                    rules,
                    df_panda[["Rule Name", "translation"]],
                    on="Rule Name",
                    how="left",
                )

                rules = rules.rename(columns={"translation": f"{clm}Translated"})
                rules[f"{clm}Translated"] = rules[f"{clm}Translated"].apply(
                    lambda x: x[0] if x is not None else None
                )

            for clm in columns:
                rules = rules.drop([clm], axis=1)
                rules = rules.rename(columns={f"{clm}Translated": clm})
        elif rules is None:
            rules = model_bpa_rules(
                dataset=dataset, workspace=workspace, dependencies=dep
            )

        rules["Severity"].replace("Warning", "⚠️", inplace=True)
        rules["Severity"].replace("Error", "\u274C", inplace=True)
        rules["Severity"].replace("Info", "ℹ️", inplace=True)

        pd.set_option("display.max_colwidth", 1000)

        violations = pd.DataFrame(columns=["Object Name", "Scope", "Rule Name"])

        scope_to_dataframe = {
            "Relationship": (
                tom.model.Relationships,
                lambda obj: create_relationship_name(
                    obj.FromTable.Name,
                    obj.FromColumn.Name,
                    obj.ToTable.Name,
                    obj.ToColumn.Name,
                ),
            ),
            "Column": (
                tom.all_columns(),
                lambda obj: format_dax_object_name(obj.Parent.Name, obj.Name),
            ),
            "Measure": (tom.all_measures(), lambda obj: obj.Name),
            "Hierarchy": (
                tom.all_hierarchies(),
                lambda obj: format_dax_object_name(obj.Parent.Name, obj.Name),
            ),
            "Table": (tom.model.Tables, lambda obj: obj.Name),
            "Role": (tom.model.Roles, lambda obj: obj.Name),
            "Model": (tom.model, lambda obj: obj.Model.Name),
            "Calculation Item": (
                tom.all_calculation_items(),
                lambda obj: format_dax_object_name(obj.Parent.Table.Name, obj.Name),
            ),
            "Row Level Security": (
                tom.all_rls(),
                lambda obj: format_dax_object_name(obj.Parent.Name, obj.Name),
            ),
            "Partition": (
                tom.all_partitions(),
                lambda obj: format_dax_object_name(obj.Parent.Name, obj.Name),
            ),
        }

        for i, r in rules.iterrows():
            ruleName = r["Rule Name"]
            expr = r["Expression"]
            scopes = r["Scope"]

            if isinstance(scopes, str):
                scopes = [scopes]

            for scope in scopes:
                func = scope_to_dataframe[scope][0]
                nm = scope_to_dataframe[scope][1]

                if scope == "Model":
                    x = []
                    if expr(func):
                        x = ["Model"]
                elif scope == "Measure":
                    x = [nm(obj) for obj in tom.all_measures() if expr(obj)]
                elif scope == "Column":
                    x = [nm(obj) for obj in tom.all_columns() if expr(obj)]
                elif scope == "Partition":
                    x = [nm(obj) for obj in tom.all_partitions() if expr(obj)]
                elif scope == "Hierarchy":
                    x = [nm(obj) for obj in tom.all_hierarchies() if expr(obj)]
                elif scope == "Table":
                    x = [nm(obj) for obj in tom.model.Tables if expr(obj)]
                elif scope == "Relationship":
                    x = [nm(obj) for obj in tom.model.Relationships if expr(obj)]
                elif scope == "Role":
                    x = [nm(obj) for obj in tom.model.Roles if expr(obj)]
                elif scope == "Row Level Security":
                    x = [nm(obj) for obj in tom.all_rls() if expr(obj)]
                elif scope == "Calculation Item":
                    x = [nm(obj) for obj in tom.all_calculation_items() if expr(obj)]

                if len(x) > 0:
                    new_data = {"Object Name": x, "Scope": scope, "Rule Name": ruleName}
                    violations = pd.concat(
                        [violations, pd.DataFrame(new_data)], ignore_index=True
                    )

        prepDF = pd.merge(
            violations,
            rules[["Rule Name", "Category", "Severity", "Description", "URL"]],
            left_on="Rule Name",
            right_on="Rule Name",
            how="left",
        )
        prepDF.rename(columns={"Scope": "Object Type"}, inplace=True)
        finalDF = prepDF[
            [
                "Category",
                "Rule Name",
                "Severity",
                "Object Type",
                "Object Name",
                "Description",
                "URL",
            ]
        ]

    if export:
        lakeAttach = lakehouse_attached()
        if lakeAttach is False:
            raise ValueError(
                f"{icons.red_dot} In order to save the Best Practice Analyzer results, a lakehouse must be attached to the notebook. Please attach a lakehouse to this notebook."
            )

        dfExport = finalDF.copy()
        delta_table_name = "modelbparesults"

        lakehouse_id = fabric.get_lakehouse_id()
        lakehouse = resolve_lakehouse_name(
            lakehouse_id=lakehouse_id, workspace=workspace
        )

        lakeT = get_lakehouse_tables(lakehouse=lakehouse, workspace=workspace)
        lakeT_filt = lakeT[lakeT["Table Name"] == delta_table_name]

        dfExport["Severity"].replace("⚠️", "Warning", inplace=True)
        dfExport["Severity"].replace("\u274C", "Error", inplace=True)
        dfExport["Severity"].replace("ℹ️", "Info", inplace=True)

        spark = SparkSession.builder.getOrCreate()
        query = f"SELECT MAX(RunId) FROM {lakehouse}.{delta_table_name}"

        if len(lakeT_filt) == 0:
            runId = 1
        else:
            dfSpark = spark.sql(query)
            maxRunId = dfSpark.collect()[0][0]
            runId = maxRunId + 1

        now = datetime.datetime.now()
        dfExport["Workspace Name"] = workspace
        dfExport["Dataset Name"] = dataset
        dfExport["Timestamp"] = now
        dfExport["RunId"] = runId

        dfExport["RunId"] = dfExport["RunId"].astype("int")

        colName = "Workspace Name"
        dfExport.insert(0, colName, dfExport.pop(colName))
        colName = "Dataset Name"
        dfExport.insert(1, colName, dfExport.pop(colName))

        dfExport.columns = dfExport.columns.str.replace(" ", "_")
        spark_df = spark.createDataFrame(dfExport)
        spark_df.write.mode("append").format("delta").saveAsTable(delta_table_name)
        print(
            f"{icons.green_dot} Model Best Practice Analyzer results for the '{dataset}' semantic model have been appended to the '{delta_table_name}' delta table."
        )

    if return_dataframe:
        return finalDF

    pd.set_option("display.max_colwidth", 100)

    finalDF = (
        finalDF[
            [
                "Category",
                "Rule Name",
                "Object Type",
                "Object Name",
                "Severity",
                "Description",
                "URL",
            ]
        ]
        .sort_values(["Category", "Rule Name", "Object Type", "Object Name"])
        .set_index(["Category", "Rule Name"])
    )

    bpa2 = finalDF.reset_index()
    bpa_dict = {
        cat: bpa2[bpa2["Category"] == cat].drop("Category", axis=1)
        for cat in bpa2["Category"].drop_duplicates().values
    }

    styles = """
    <style>
        .tab { overflow: hidden; border: 1px solid #ccc; background-color: #f1f1f1; }
        .tab button { background-color: inherit; float: left; border: none; outline: none; cursor: pointer; padding: 14px 16px; transition: 0.3s; }
        .tab button:hover { background-color: #ddd; }
        .tab button.active { background-color: #ccc; }
        .tabcontent { display: none; padding: 6px 12px; border: 1px solid #ccc; border-top: none; }
        .tabcontent.active { display: block; }
        .tooltip { position: relative; display: inline-block; }
        .tooltip .tooltiptext { visibility: hidden; width: 300px; background-color: #555; color: #fff; text-align: center; border-radius: 6px; padding: 5px; position: absolute; z-index: 1; bottom: 125%; left: 50%; margin-left: -110px; opacity: 0; transition: opacity 0.3s; }
        .tooltip:hover .tooltiptext { visibility: visible; opacity: 1; }
    </style>
    """

    # JavaScript for tab functionality
    script = """
    <script>
    function openTab(evt, tabName) {
        var i, tabcontent, tablinks;
        tabcontent = document.getElementsByClassName("tabcontent");
        for (i = 0; i < tabcontent.length; i++) {
            tabcontent[i].style.display = "none";
        }
        tablinks = document.getElementsByClassName("tablinks");
        for (i = 0; i < tablinks.length; i++) {
            tablinks[i].className = tablinks[i].className.replace(" active", "");
        }
        document.getElementById(tabName).style.display = "block";
        evt.currentTarget.className += " active";
    }
    </script>
    """

    # JavaScript for dynamic tooltip positioning
    dynamic_script = """
    <script>
    function adjustTooltipPosition(event) {
        var tooltip = event.target.querySelector('.tooltiptext');
        var rect = tooltip.getBoundingClientRect();
        var topSpace = rect.top;
        var bottomSpace = window.innerHeight - rect.bottom;

        if (topSpace < bottomSpace) {
            tooltip.style.bottom = '125%';
        } else {
            tooltip.style.bottom = 'auto';
            tooltip.style.top = '125%';
        }
    }
    </script>
    """

    # HTML for tabs
    tab_html = '<div class="tab">'
    content_html = ""
    for i, (title, df) in enumerate(bpa_dict.items()):
        if df.shape[0] == 0:
            continue

        tab_id = f"tab{i}"
        active_class = ""
        if i == 0:
            active_class = "active"

        summary = " + ".join(
            [f"{idx} ({v})" for idx, v in df["Severity"].value_counts().items()]
        )
        tab_html += f'<button class="tablinks {active_class}" onclick="openTab(event, \'{tab_id}\')"><b>{title}</b><br/>{summary}</button>'
        content_html += f'<div id="{tab_id}" class="tabcontent {active_class}">'

        # Adding tooltip for Rule Name using Description column
        content_html += '<table border="1">'
        content_html += "<tr><th>Rule Name</th><th>Object Type</th><th>Object Name</th><th>Severity</th></tr>"
        for _, row in df.iterrows():
            content_html += "<tr>"
            if pd.notnull(row["URL"]):
                content_html += f'<td class="tooltip" onmouseover="adjustTooltipPosition(event)"><a href="{row["URL"]}">{row["Rule Name"]}</a><span class="tooltiptext">{row["Description"]}</span></td>'
            elif pd.notnull(row["Description"]):
                content_html += f'<td class="tooltip" onmouseover="adjustTooltipPosition(event)">{row["Rule Name"]}<span class="tooltiptext">{row["Description"]}</span></td>'
            else:
                content_html += f'<td>{row["Rule Name"]}</td>'
            content_html += f'<td>{row["Object Type"]}</td>'
            content_html += f'<td>{row["Object Name"]}</td>'
            content_html += f'<td>{row["Severity"]}</td>'
            content_html += "</tr>"
        content_html += "</table>"

        content_html += "</div>"
    tab_html += "</div>"

    # Display the tabs, tab contents, and run the script
    return display(HTML(styles + tab_html + content_html + script))
