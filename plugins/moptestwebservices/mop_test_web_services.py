# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# oq-qgis-server-plugins
# Copyright (C) 2019 GEM Foundation
#
# oq-geoviewer is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# oq-geoviewer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import time
from qgis.server import QgsService
from qgis.core import (
    QgsProject, QgsVectorFileWriter, QgsVectorLayer,
    QgsPointXY, QgsField, QgsFeatureRequest, QgsFeature, QgsGeometry,
    )  # QgsPoint)
from qgis.PyQt.QtCore import QVariant  # QMetaType, QSettings

# from qgis.PyQt.QtCore import pyqtRemoveInputHook
# def _mute():
#     pyqtRemoveInputHook()

# import pdb ; _mute() ; pdb.set_trace()


class MOPTEST(QgsService):
    def __init__(self):
        QgsService.__init__(self)

    def name(self):
        return "MOPTEST"

    def version(self):
        return "1.0.0"

    def allowMethod(method):
        return True

    def executeRequest(self, request, response, project):
        # Get the project instance
        project = QgsProject.instance()
        # Print the current project file name (might be
        # empty in case no projects have been loaded)
        # print(project.fileName())


        # Pulisci il progetto corrente (opzionale, ma consigliato per evitare conflitti)
        project.clear()

        # Load another project
        project.read(
            '/home/nastasi/git/oq-geoviewer'
            '/project_samples/papers/PapersTmpl.qgs')
        print(project.fileName())

        # New vector layer initialization
        layer = QgsVectorLayer("Point", "NuovoLayer", "memory")

        # Add fields to the layer
        layer.dataProvider().addAttributes([
            QgsField("id", QVariant.Int),
            QgsField("nome", QVariant.String)
        ])
        layer.updateFields()

        # Create some sample features
        feature1 = QgsFeature()
        feature1.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(10, 10)))
        feature1.setAttributes([1, "Punto 1"])

        feature2 = QgsFeature()
        feature2.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(20, 20)))
        feature2.setAttributes([2, "Punto 2"])

        # Add features to the layer
        layer.dataProvider().addFeatures([feature1, feature2])

        # Update the layer extension
        layer.updateExtents()

        # Save layer as GeoPackage
        save_options = QgsVectorFileWriter.SaveVectorOptions()
        save_options.driverName = "GPKG"
        layer_name = "NuovoLayer"
        save_options.layerName = layer_name
        gpkg_filepath = ('/home/nastasi/git/oq-geoviewer'
                         '/project_samples/papers/out/Papers02_layer.gpkg')
        error = QgsVectorFileWriter.writeAsVectorFormat(
            layer, gpkg_filepath,
            "UTF-8", layer.crs(), "GPKG", layerOptions=['OVERWRITE=YES'])

        if error[0] == QgsVectorFileWriter.NoError:
            print("Layer save: success")
        else:
            print("Layer save: error:", error)

        gpkg_layer = QgsVectorLayer(gpkg_filepath, layer_name, 'ogr')

        # add gpkg layer to current QGIS project
        QgsProject.instance().addMapLayer(gpkg_layer)

        project.write(
            '/home/nastasi/git/oq-geoviewer'
            '/project_samples/papers/out/Papers03.qgz')

        response.setStatusCode(200)
        response.write(
            json.dumps({'owner': 'mop', 'test': project.fileName()},
                       indent=4, sort_keys=True))


class MOPTEST_REG():

    def __init__(self, serverIface):
        self.serv = MOPTEST()
        serverIface.serviceRegistry().registerService(MOPTEST())
