# GenFill menu registration (GUI sessions only).
import nuke

if nuke.GUI:
    _toolbar = nuke.menu("Nodes")
    _menu = _toolbar.addMenu("GenFill", icon="GenFill.png")
    # Tab-search "Generative Fill" also finds this.
    _menu.addCommand("Generative Fill", "import genfill.node; genfill.node.create()",
                     icon="GenFill.png")
    _menu.addSeparator()
    _menu.addCommand("API Keys...", "import genfill.ui.settings; genfill.ui.settings.show()")
    _menu.addCommand("Check for Updates...", "import genfill.updates; genfill.updates.check_for_updates()")
    _local = _menu.addMenu("Local AI")
    _local.addCommand("Install / Repair...", "import genfill.ui.localai_dialog as d; d.show()")
    _local.addCommand("Stop AI Helper (free GPU memory)",
                      "import genfill.localai.client as c; c.stop_helper()")
    _local.addCommand("Open Local AI Folder",
                      "import os, genfill.localai as l; os.startfile(l.root()) if os.path.isdir(l.root()) "
                      "else nuke.message('Local AI is not installed yet.')")
    # Same commands in Nuke's top menu bar, which is easier to find.
    _top = nuke.menu("Nuke").addMenu("GenFill")
    _top.addCommand("Generative Fill", "import genfill.node; genfill.node.create()")
    _top.addSeparator()
    _top.addCommand("Local AI: Install / Repair...", "import genfill.ui.localai_dialog as d; d.show()")
    _top.addCommand("Local AI: Stop AI Helper (free GPU memory)",
                    "import genfill.localai.client as c; c.stop_helper()")
    _top.addCommand("API Keys...", "import genfill.ui.settings; genfill.ui.settings.show()")
    _top.addCommand("Check for Updates...", "import genfill.updates; genfill.updates.check_for_updates()")
    _top.addSeparator()
    _top.addCommand("Developer/Run Smoke Test", "import genfill.devtools; genfill.devtools.run_smoke_test()")
    _top.addCommand("Developer/Reload GenFill", "import genfill.devtools; genfill.devtools.reload_genfill()")
    _top.addCommand("Developer/Build Info...", "import genfill.devtools; genfill.devtools.show_build_info()")
    _dev = _menu.addMenu("Developer")
    _dev.addCommand("Run Smoke Test", "import genfill.devtools; genfill.devtools.run_smoke_test()")
    _dev.addCommand("Reload GenFill (after installing a new build)",
                    "import genfill.devtools; genfill.devtools.reload_genfill()")
    _dev.addCommand("Build Info...", "import genfill.devtools; genfill.devtools.show_build_info()")
