#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  king_phisher/client/tabs/campaign.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import collections
import logging
import os
import threading
import time
import urlparse

from king_phisher import utilities
from king_phisher.client import export
from king_phisher.client import graphs
from king_phisher.client import gui_utilities
from king_phisher.client.mailer import MailSenderThread

from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import Gtk

class CampaignViewGenericTab(gui_utilities.UtilityGladeGObject):
	"""
	This object is meant to be subclassed by tabs which will display
	campaign information of different types from a specific database
	table. The data in this object is refreshed when multiple events
	occur and it uses an internal timer to represent the last time the
	data was refreshed.
	"""
	gobject_ids = [
		'button_refresh',
		'treeview_campaign'
	]
	top_gobject = 'box'
	remote_table_name = ''
	"""The database table represented by this tab."""
	label_text = 'Unknown'
	"""The label of the tab for display in the GUI."""
	view_columns = {}
	"""The dictionary map of column numbers to column names starting at column 1."""
	def __init__(self, *args, **kwargs):
		self.label = Gtk.Label(self.label_text)
		"""The :py:class:`Gtk.Label` representing this tab with text from :py:attr:`~.CampaignViewGenericTab.label_text`."""
		super(CampaignViewGenericTab, self).__init__(*args, **kwargs)
		treeview = self.gobjects['treeview_campaign']
		treeview.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
		popup_copy_submenu = Gtk.Menu.new()
		self.view_column_renderers = {}
		columns = self.view_columns
		for column_id in range(1, len(columns) + 1):
			column_name = columns[column_id]
			column = Gtk.TreeViewColumn(column_name, Gtk.CellRendererText(), text=column_id)
			column.set_sort_column_id(column_id)
			treeview.append_column(column)
			self.view_column_renderers[column_id] = column

			menu_item = Gtk.MenuItem.new_with_label(column_name)
			menu_item.connect('activate', self.signal_activate_popup_menu_copy, column_id)
			popup_copy_submenu.append(menu_item)
		self.last_load_time = float('-inf')
		"""The last time the data was loaded from the server."""
		self.refresh_frequency = utilities.timedef_to_seconds(str(self.config.get('gui.refresh_frequency', '5m')))
		"""The lifetime in seconds to wait before refreshing the data from the server."""
		self.row_loader_thread = None
		"""The thread object which loads the data from the server."""
		self.row_loader_thread_lock = threading.Lock()
		"""The :py:class:`threading.Lock` object used for synchronization between the loader and main threads."""
		self.is_destroyed = threading.Event()
		getattr(self, self.top_gobject).connect('destroy', self.signal_destroy)

		self.popup_menu = Gtk.Menu.new()
		"""The :py:class:`Gtk.Menu` object which is displayed when right-clicking in the view area."""
		menu_item = Gtk.MenuItem.new_with_label('Copy')
		menu_item.set_submenu(popup_copy_submenu)
		self.popup_menu.append(menu_item)

		menu_item = Gtk.SeparatorMenuItem()
		self.popup_menu.append(menu_item)

		menu_item = Gtk.MenuItem.new_with_label('Delete')
		menu_item.connect('activate', lambda _: self._prompt_to_delete_row())
		self.popup_menu.append(menu_item)
		self.popup_menu.show_all()

	def _prompt_to_delete_row(self):
		if isinstance(self.row_loader_thread, threading.Thread) and self.row_loader_thread.is_alive():
			gui_utilities.show_dialog_warning('Can Not Delete Rows While Loading', self.parent)
			return
		treeview = self.gobjects['treeview_campaign']
		selection = treeview.get_selection()
		(model, tree_iter) = selection.get_selected()
		if not tree_iter:
			return
		row_id = model.get_value(tree_iter, 0)
		if not gui_utilities.show_dialog_yes_no('Delete This Row?', self.parent, 'This information will be lost.'):
			return
		self.parent.rpc(self.remote_table_name + '/delete', row_id)
		self.load_campaign_information(force=True)

	def format_row_data(self, row):
		"""
		This method is overridden by subclasses to format the raw row
		data returned from the server. The length of the list must equal
		the number of columns in the table. This method is called for
		each row in the remote table by the loader thread.

		:return: The formated row data.
		:rtype: list
		"""
		raise NotImplementedError()

	def load_campaign_information(self, force=False):
		"""
		Load the necessary campaign information from the remote server.
		Unless *force* is True, the
		:py:attr:`~.CampaignViewGenericTab.last_load_time` is compared
		with the :py:attr:`~.CampaignViewGenericTab.refresh_frequency` to
		check if the information is stale. If the local data is not stale,
		this function will return without updating the table.

		:param bool force: Ignore the load life time and force loading the remote data.
		"""
		if not force and ((time.time() - self.last_load_time) < self.refresh_frequency):
			return
		if isinstance(self.row_loader_thread, threading.Thread) and self.row_loader_thread.is_alive():
			return
		self.row_loader_thread_lock.acquire()
		treeview = self.gobjects['treeview_campaign']
		store = treeview.get_model()
		if store == None:
			store_columns = [str]
			map(lambda x: store_columns.append(str), range(len(self.view_columns)))
			store = Gtk.ListStore(*store_columns)
			treeview.set_model(store)
		else:
			store.clear()
		self.row_loader_thread = threading.Thread(target=self.row_loader_thread_routine, args=(store,))
		self.row_loader_thread.start()
		self.row_loader_thread_lock.release()
		return

	def row_loader_thread_routine(self, store):
		"""
		The loading routine to be executed within a thread.

		:param store: The store object to place the new data.
		:type store: :py:class:`Gtk.ListStore`
		"""
		gui_utilities.glib_idle_add_wait(lambda: self.gobjects['treeview_campaign'].set_property('sensitive', False))
		for row_data in self.parent.rpc.remote_table('campaign/' + self.remote_table_name, self.config['campaign_id']):
			row_id = row_data['id']
			row_data = self.format_row_data(row_data)
			if row_data == None:
				self.parent.rpc(self.remote_table_name + '/delete', row_id)
				continue
			row_data = map(lambda x: '' if x == None else str(x), row_data)
			row_data.insert(0, str(row_id))
			if self.is_destroyed.is_set():
				return
			gui_utilities.glib_idle_add_wait(store.append, row_data)
		gui_utilities.glib_idle_add_wait(lambda: self.gobjects['treeview_campaign'].set_property('sensitive', True))
		self.last_load_time = time.time()

	def signal_button_clicked_refresh(self, button):
		self.load_campaign_information(force=True)

	def signal_button_clicked_export(self, button):
		if isinstance(self.row_loader_thread, threading.Thread) and self.row_loader_thread.is_alive():
			gui_utilities.show_dialog_warning('Can Not Export Rows While Loading', self.parent)
			return
		dialog = gui_utilities.UtilityFileChooser('Export Data', self.parent)
		file_name = self.config['campaign_name'] + '.csv'
		response = dialog.run_quick_save(file_name)
		dialog.destroy()
		if not response:
			return
		destination_file = response['target_path']
		export.treeview_liststore_to_csv(self.gobjects['treeview_campaign'], destination_file)

	def signal_destroy(self, gobject):
		self.is_destroyed.set()
		if isinstance(self.row_loader_thread, threading.Thread) and self.row_loader_thread.is_alive():
			self.row_loader_thread.join()

	def signal_treeview_button_pressed(self, widget, event):
		if not (event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3):
			return
		selection = self.gobjects['treeview_campaign'].get_selection()
		if not selection.get_selected()[1]:
			return
		pos_func = lambda m, d: (event.get_root_coords()[0], event.get_root_coords()[1], True)
		self.popup_menu.popup(None, None, pos_func, None, event.button, event.time)
		return True

	def signal_treeview_key_pressed(self, widget, event):
		if event.type != Gdk.EventType.KEY_PRESS:
			return
		keyval = event.get_keyval()[1]
		if keyval == Gdk.KEY_F5:
			self.load_campaign_information(force=True)
		elif keyval == Gdk.KEY_Delete:
			self._prompt_to_delete_row()

	def signal_activate_popup_menu_copy(self, widget, column_id):
		treeview = self.gobjects['treeview_campaign']
		selection = treeview.get_selection()
		(model, tree_iter) = selection.get_selected()
		if not tree_iter:
			return
		selection_text = model.get_value(tree_iter, column_id)
		clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
		clipboard.set_text(selection_text, -1)

class CampaignViewDeaddropTab(CampaignViewGenericTab):
	"""Display campaign information regarding dead drop connections."""
	remote_table_name = 'deaddrop_connections'
	label_text = 'Deaddrop'
	view_columns = {
		1: 'Destination',
		2: 'Visit Count',
		3: 'External IP',
		4: 'Username',
		5: 'Hostname',
		6: 'Local IP Addresses',
		7: 'First Hit',
		8: 'Last Hit'
	}
	def format_row_data(self, connection):
		deploy_id = connection['deployment_id']
		deploy_details = self.parent.rpc.remote_table_row('deaddrop_deployments', deploy_id, cache=True)
		if not deploy_details:
			return None
		row = (
			deploy_details['destination'],
			connection['visit_count'],
			connection['visitor_ip'],
			connection['local_username'],
			connection['local_hostname'],
			connection['local_ip_addresses'],
			utilities.format_datetime(connection['first_visit']),
			utilities.format_datetime(connection['last_visit'])
		)
		return row

class CampaignViewCredentialsTab(CampaignViewGenericTab):
	"""Display campaign information regarding submitted credentials."""
	remote_table_name = 'credentials'
	label_text = 'Credentials'
	view_columns = {
		1: 'Email',
		2: 'Username',
		3: 'Password',
		4: 'Submitted'
	}
	def __init__(self, *args, **kwargs):
		super(CampaignViewCredentialsTab, self).__init__(*args, **kwargs)
		self.view_column_renderers[3].set_property('visible', False)

	def format_row_data(self, credential):
		msg_id = credential['message_id']
		msg_details = self.parent.rpc.remote_table_row('messages', msg_id, cache=True)
		if not msg_details:
			return None
		row = (
			msg_details['target_email'],
			credential['username'],
			credential['password'],
			utilities.format_datetime(credential['submitted'])
		)
		return row

	def signal_button_toggled_show_passwords(self, button):
		self.view_column_renderers[3].set_property('visible', button.get_property('active'))

class CampaignViewDashboardTab(gui_utilities.UtilityGladeGObject):
	"""Display campaign information on a graphical dash board."""
	gobject_ids = [
		'box_top_left',
		'box_top_right',
		'box_bottom',
		'scrolledwindow_top_left',
		'scrolledwindow_top_right',
		'scrolledwindow_bottom'
	]
	top_gobject = 'box'
	label_text = 'Dashboard'
	"""The tabs label for display in the GUI."""
	def __init__(self, *args, **kwargs):
		self.label = Gtk.Label(self.label_text)
		"""The :py:class:`Gtk.Label` representing this tab with text from :py:attr:`~.CampaignViewDashboardTab.label_text`."""
		super(CampaignViewDashboardTab, self).__init__(*args, **kwargs)
		self.last_load_time = float('-inf')
		"""The last time the data was loaded from the server."""
		self.refresh_frequency = utilities.timedef_to_seconds(str(self.config.get('gui.refresh_frequency', '5m')))
		"""The lifetime in seconds to wait before refreshing the data from the server."""
		self.loader_thread = None
		"""The thread object which loads the data from the server."""
		self.loader_thread_lock = threading.RLock()
		"""The :py:class:`threading.Lock` object used for synchronization between the loader and main threads."""
		self.graphs = []
		"""The :py:class:`.CampaignGraph` classes represented on the dash board."""

		self.logger.debug("dashboard refresh frequency set to {0} seconds".format(self.refresh_frequency))
		# Position: (DefaultGraphName, Size)
		dash_ports = {
			'top_left': (380, 200),
			'top_right': (380, 200),
			'bottom': None
		}
		for dash_port, details in dash_ports.items():
			graph_name = self.config['dashboard.' + dash_port]
			Klass = graphs.get_graph(graph_name)
			if not Klass:
				self.logger.warning('could not get graph: ' + graph_name)
				continue
			graph_inst = Klass(self.config, self.parent, details)
			self.gobjects['scrolledwindow_' + dash_port].add_with_viewport(graph_inst.canvas)
			self.gobjects['box_' + dash_port].pack_end(graph_inst.navigation_toolbar, False, False, 0)
			self.graphs.append(graph_inst)
		GLib.timeout_add_seconds(self.refresh_frequency, self.loader_idle_routine)

	def load_campaign_information(self, force=False):
		"""
		Load the necessary campaign information from the remote server.
		Unless *force* is True, the
		:py:attr:`~.CampaignViewDashboardTab.last_load_time` is compared
		with the :py:attr:`~.CampaignViewDashboardTab.refresh_frequency` to
		check if the information is stale. If the local data is not
		stale, this function will return without updating the table.

		:param bool force: Ignore the load life time and force loading the remote data.
		"""
		if not force and ((time.time() - self.last_load_time) < self.refresh_frequency):
			return
		if not hasattr(self.parent, 'rpc'):
			self.logger.warning('skipping load_campaign_information because rpc is not initialized')
			return
		with self.loader_thread_lock:
			if isinstance(self.loader_thread, threading.Thread) and self.loader_thread.is_alive():
				return
			self.loader_thread = threading.Thread(target=self.loader_thread_routine)
			self.loader_thread.start()

	def loader_idle_routine(self):
		"""The routine which refreshes the campaign data at a regular interval."""
		self.logger.debug('idle loader routine called')
		self.load_campaign_information(force=True)
		return True

	def loader_thread_routine(self):
		"""The loading routine to be executed within a thread."""
		info_cache = {}
		for graph in self.graphs:
			info_cache = gui_utilities.glib_idle_add_wait(graph.refresh, info_cache)
		self.last_load_time = time.time()

	def signal_button_clicked_refresh(self, button):
		self.load_campaign_information(force=True)

class CampaignViewVisitsTab(CampaignViewGenericTab):
	"""Display campaign information regarding incoming visitors."""
	remote_table_name = 'visits'
	label_text = 'Visits'
	view_columns = {
		1: 'Email',
		2: 'Visitor IP',
		3: 'Visitor Details',
		4: 'Visit Count',
		5: 'First Visit',
		6: 'Last Visit'
	}
	def format_row_data(self, visit):
		msg_id = visit['message_id']
		msg_details = self.parent.rpc.remote_table_row('messages', msg_id, cache=True)
		if not msg_details:
			return None
		row = (
			msg_details['target_email'],
			visit['visitor_ip'],
			visit['visitor_details'],
			visit['visit_count'],
			utilities.format_datetime(visit['first_visit']),
			utilities.format_datetime(visit['last_visit'])
		)
		return row

class CampaignViewMessagesTab(CampaignViewGenericTab):
	"""Display campaign information regarding sent messages."""
	remote_table_name = 'messages'
	label_text = 'Messages'
	view_columns = {
		1: 'Email',
		2: 'Sent',
		3: 'Opened',
		4: 'Trained'
	}
	def format_row_data(self, message):
		row = (
			message['target_email'],
			utilities.format_datetime(message['sent']),
			utilities.format_datetime(message['opened']),
			('Yes' if message['trained'] else '')
		)
		return row

class CampaignViewTab(object):
	"""
	The King Phisher client top-level 'View Campaign' tab. This object
	manages the sub-tabs which display all the information regarding
	the current campaign.
	"""
	def __init__(self, config, parent):
		"""
		:param dict config: The King Phisher client configuration.
		:param parent: The parent window for this object.
		:type parent: :py:class:`Gtk.Window`
		"""
		self.config = config
		self.parent = parent
		self.logger = logging.getLogger('KingPhisher.Client.' + self.__class__.__name__)
		self.box = Gtk.Box()
		self.box.set_property('orientation', Gtk.Orientation.VERTICAL)
		self.box.show()
		self.label = Gtk.Label('View Campaign')
		"""The :py:class:`Gtk.Label` representing this tabs name."""

		self.notebook = Gtk.Notebook()
		""" The :py:class:`Gtk.Notebook` for holding sub-tabs."""
		self.notebook.connect('switch-page', self._tab_changed)
		self.notebook.set_scrollable(True)
		self.box.pack_start(self.notebook, True, True, 0)

		self.tabs = {}
		"""A dict object holding the sub tabs managed by this object."""
		current_page = self.notebook.get_current_page()
		self.last_page_id = current_page

		if graphs.has_matplotlib:
			self.logger.info('matplotlib is installed, dashboard will be available')
			dashboard_tab = CampaignViewDashboardTab(self.config, self.parent)
			self.tabs['dashboard'] = dashboard_tab
			self.notebook.append_page(dashboard_tab.box, dashboard_tab.label)
		else:
			self.logger.warning('matplotlib is not installed, dashboard will not be available')

		messages_tab = CampaignViewMessagesTab(self.config, self.parent)
		self.tabs['messages'] = messages_tab
		self.notebook.append_page(messages_tab.box, messages_tab.label)

		visits_tab = CampaignViewVisitsTab(self.config, self.parent)
		self.tabs['visits'] = visits_tab
		self.notebook.append_page(visits_tab.box, visits_tab.label)

		credentials_tab = CampaignViewCredentialsTab(self.config, self.parent)
		self.tabs['credentials'] = credentials_tab
		self.notebook.append_page(credentials_tab.box, credentials_tab.label)

		deaddrop_connections_tab = CampaignViewDeaddropTab(self.config, self.parent)
		self.tabs['deaddrop_connections'] = deaddrop_connections_tab
		self.notebook.append_page(deaddrop_connections_tab.box, deaddrop_connections_tab.label)

		for tab in self.tabs.values():
			tab.box.show()
		self.notebook.show()
		self.parent.connect('campaign-set', self.signal_kpc_campaign_set)

	def signal_kpc_campaign_set(self, kpc, cid):
		for tab_name, tab in self.tabs.items():
			if hasattr(tab, 'load_campaign_information'):
				tab.load_campaign_information(force=True)

	def _tab_changed(self, notebook, current_page, index):
		if not hasattr(self.parent, 'rpc'):
			return
		previous_page = notebook.get_nth_page(self.last_page_id)
		self.last_page_id = index

		for tab_name, tab in self.tabs.items():
			if current_page != tab.box:
				continue
			if hasattr(tab, 'load_campaign_information'):
				tab.load_campaign_information()
