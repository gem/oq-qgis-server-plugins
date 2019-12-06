# -*- coding: utf-8 -*-
"""
/***************************************************************************
    QGIS Server Plugin Filters: Add a new request to call ApiService QGIS server
    ---------------------
    Date                 : February 2019
    Copyright            : (C) 2019 by Marco Bernasocchi - OPENGIS.ch
    Email                : marco at opengis.ch
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

__author__ = 'Marco Bernasocchi'
__date__ = 'February 2019'
__copyright__ = '(C) 2019, Marco Bernasocchi - OPENGIS.ch'

from qgis.core import QgsMessageLog, Qgis

from  api.api_service import ApiService
from api.api_filter import ApiFilter

class ApiServer:
    """Plugin for QGIS server
    this plugin loads api filter"""

    def __init__(self, serverIface):
        # Save reference to the QGIS server interface
        self.serverIface = serverIface
        QgsMessageLog.logMessage("SUCCESS - api init plugin", level=Qgis.Info)

        #register service
        self.serv = ApiService()
        serverIface.serviceRegistry().registerService(ApiService())

        # add filters
        try:
            serverIface.registerFilter(ApiFilter(serverIface), 50)
        except Exception as e:
            QgsMessageLog.logMessage("api - Error loading filter: %s" % e )
