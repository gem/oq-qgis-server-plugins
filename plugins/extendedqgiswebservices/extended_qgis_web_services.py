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

import io
import re
import os
import csv
import math
import numpy
import json
import tempfile
from zipfile import ZipFile
import bisect
from requests import Session

from qgis.core import Qgis
from qgis.server import QgsService, QgsServerProjectUtils, QgsServerSettings
from qgis.core import (
    QgsRasterLayer, QgsProject, QgsVectorLayer, QgsVectorFileWriter,
    QgsField, edit, QgsFeature, QgsPointXY, QgsGeometry,
    QgsReferencedRectangle, QgsSymbol, QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer, QgsRuleBasedRenderer, QgsCoordinateTransform,
    QgsApplication, QgsStyle, QgsFillSymbol, NULL,
    QgsWkbTypes, QgsClassificationJenks, QgsClassificationRange,
)

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import QVariant, QSize, QUrl, QUrlQuery
from qgis.gui import QgsMapCanvas

import processing

from .svir_utils.shared import RAMP_EXTREME_COLORS
from .svir_utils.utils import get_style

# FIXME reenable folder deletion after zip creation
from .gem_common import gem_log, rmdir_recursive, alphanum_rndstr
from .lock import acquire_lock, release_lock
from .zipdir import zipdir

import xml.etree.ElementTree as ET

if Qgis.QGIS_VERSION_INT < 31000:
    # the following is an enum
    DEFAULT_STYLE_MODE = QgsGraduatedSymbolRenderer.Quantile
else:
    # Quantile is the id of QgsClassificationQuantile
    DEFAULT_STYLE_MODE = 'Quantile'


DEFAULT_SETTINGS = dict(
    color_from_rgba=QColor('#FFEBEB').rgba(),
    color_to_rgba=QColor('red').rgba(),
    style_mode=DEFAULT_STYLE_MODE,
    style_classes=10,
    force_restyling=True,
    experimental_enabled=False,
    developer_mode=False,
    log_level='C',
)

#
# NOTE: _get_material and _get_occupancy will be added as part of
#       the oq-gem-taxonomy python library in the new query swiss-knife function
#
def _get_material(taxonomy):
    mat = re.findall('^[A-Z0-9]+', taxonomy)
    if len(mat) < 1:
        return 'UNK'
    return mat[0]

def _get_occupancy(taxonomy):
    occ = re.findall('[^A-Z0-9]?(RES|COM|MIX|IND|AGR|GOV|EDU'
                     '|HEA|TRT|OCO)[^A-Z[0-9]?', taxonomy)
    if len(occ) < 1:
        return 'UNK'
    return occ[0]

def _create_layer_from_selected(source_layer, with_attributes=False):
    """Create a new layer containing only selected features"""
    if source_layer.selectedFeatureCount() == 0:
        print("No features selected!")
        return None

    # Create memory layer with same CRS and geometry type
    geom_type = QgsWkbTypes.displayString(source_layer.wkbType())
    crs = source_layer.crs().authid()
    temp_layer = QgsVectorLayer(f'{geom_type}?crs={crs}', 'selected_features', 'memory')

    temp_provider = temp_layer.dataProvider()
    temp_provider.createSpatialIndex()
    if with_attributes:
        temp_layer.startEditing()
        temp_provider.addAttributes(source_layer.fields())
        temp_layer.updateFields()
        temp_layer.commitChanges()

    # Copy selected features
    selected_features = list(source_layer.getSelectedFeatures())
    temp_provider.addFeatures(selected_features)
    temp_layer.updateExtents()

    return temp_layer

def _style_curves(layer, style_by):
    use_sgc_style = False
    opacity = 1.0

    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    symbol.setOpacity(opacity)

    style = get_style(layer, None)

    ramp = QgsGradientColorRamp(
        style['color_from'], style['color_to'])

    style_mode = style['style_mode']

    default_qgs_style = QgsStyle().defaultStyle()
    default_color_ramp_names = default_qgs_style.colorRampNames()

    style_mode = 'EqualInterval'

    ramp_type_idx = default_color_ramp_names.index('Spectral')
    inverted = True
    symbol.setColor(QColor(RAMP_EXTREME_COLORS['Reds']['top']))

    ramp = default_qgs_style.colorRamp(
        default_color_ramp_names[ramp_type_idx])

    symbol.setColor(QColor(RAMP_EXTREME_COLORS['Reds']['top']))

    if inverted:
        ramp.invert()

    # get unique values
    fni = layer.fields().indexOf(style_by)
    unique_values = layer.dataProvider().uniqueValues(fni)
    num_unique_values = len(unique_values - {NULL})
    gem_log('num_uniq: %d, QGIS vers: %d' % (
        num_unique_values, Qgis.QGIS_VERSION_INT), Qgis.Critical)

    if num_unique_values > 2:
        if Qgis.QGIS_VERSION_INT < 31000:
            renderer = QgsGraduatedSymbolRenderer.createRenderer(
                layer,
                style_by,
                min(num_unique_values, style['classes']),
                style_mode,
                symbol.clone(),
                ramp)
        else:
            renderer = QgsGraduatedSymbolRenderer(
                style_by, [])
            # NOTE: the following returns an instance of one of the
            #       subclasses of QgsClassificationMethod
            classification_method = \
                QgsApplication.classificationMethodRegistry().method(
                    style_mode)
            renderer.setClassificationMethod(classification_method)
            renderer.updateColorRamp(ramp)
            renderer.updateSymbols(symbol.clone())
            renderer.updateClasses(
                layer, min(num_unique_values, style['classes']))
            print('post updateClasses')
        if not use_sgc_style:
            if Qgis.QGIS_VERSION_INT < 31000:
                label_format = renderer.labelFormat()
                # NOTE: the following line might be useful
                # label_format.setTrimTrailingZeroes(True)
                label_format.setPrecision(2)
                renderer.setLabelFormat(label_format, updateRanges=True)
            else:
                print('not use_sgc_style')
                renderer.classificationMethod().setLabelPrecision(2)
                renderer.calculateLabelPrecision()

    layer.setRenderer(renderer)
    layer.setOpacity(opacity)


class EWMS(QgsService):

    def __init__(self):
        QgsService.__init__(self)

    def name(self):
        return "EWMS"

    def version(self):
        return "1.0.0"

    def allowMethod(method):
        return True

    def executeRequest(self, request, response, project):
        if request.parameters()['REQUEST'] == 'GetLayerNames':
            try:
                self._get_layer_names(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayersCustomProperties':
            try:
                self._get_custom_properties_by_layers(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetProjectCustomProperties':
            try:
                self._get_custom_properties_by_project(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayerFields':
            try:
                self._get_fields_by_layer(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayerStyles':
            try:
                self._get_styles_by_layer(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'OQEngine2Map':
            try:
                self._oq_engine_calc2map(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
                response.close()
        else:
            response.setStatusCode(400)
            response.write("Missing or invalid 'REQUEST' parameter")

    def _get_layer_names(self, request, response, project):
        layer_names = [
            layer.name() for id, layer in project.mapLayers().items()]
        response.setStatusCode(200)
        response.write(
            json.dumps(layer_names, indent=4, sort_keys=True))

    def _get_custom_properties_by_layers(self, request, response, project):
        if QgsServerProjectUtils.wmsUseLayerIds(project):
            dict_key = 'id'
        else:
            dict_key = 'name'
        try:
            layer_keys_str = request.parameters()['LAYERS']
        except KeyError:
            layer_keys_str = None
        try:
            filter_str = request.parameters()['FILTER']
        except KeyError:
            filter_str = None
        if layer_keys_str:
            layer_keys = layer_keys_str.split(',')
        else:
            layer_keys = None
        if filter_str:
            filter_prop_items = [filter_prop.split(':')
                                 for filter_prop in filter_str.split(',')]
            filter_props = {
                filter_prop_name: filter_prop_value
                for filter_prop_name, filter_prop_value in filter_prop_items}
        else:
            filter_props = None
        custom_props = {}
        for layer_id, layer in project.mapLayers().items():
            # If a shortname is set, we must use it instead of
            # the plain layer name
            layer_name = layer.shortName() or layer.name()
            if dict_key == 'name':
                custom_props_key = layer_name
            else:
                custom_props_key = layer_id
            if layer_keys:
                if dict_key == 'name' and layer_name not in layer_keys:
                    continue
                if dict_key == 'id' and layer_id not in layer_keys:
                    continue
            custom_props[custom_props_key] = {}
            if dict_key == 'name':
                custom_props[layer_name]['layer_id'] = layer_id
            else:
                custom_props[layer_id]['layer_name'] = layer_name
            for prop in layer.customPropertyKeys():
                prop_value = layer.customProperty(prop)
                custom_props[custom_props_key][prop] = prop_value
        custom_props_filtered = custom_props.copy()
        if filter_props:
            for filter_prop in filter_props:
                for layer in custom_props:
                    filter_prop_value = filter_props[filter_prop]
                    if custom_props[layer][filter_prop] != filter_prop_value:
                        del custom_props_filtered[layer]
        response.setStatusCode(200)
        response.write(
            json.dumps(custom_props_filtered, indent=4, sort_keys=True))

    def _get_custom_properties_by_project(self, request, response, project):
        response.setStatusCode(200)
        response.write(
            json.dumps(project.customVariables(), indent=4, sort_keys=True))

    def _get_fields_by_layer(self, request, response, project):
        if QgsServerProjectUtils.wmsUseLayerIds(project):
            dict_key = 'id'
        else:
            dict_key = 'name'
        try:
            layer_keys_str = request.parameters()['LAYERS']
        except KeyError:
            layer_keys_str = None
        if layer_keys_str:
            layer_keys = layer_keys_str.split(',')
        else:
            layer_keys = None
        fields_by_layer = {}
        for layer_id, layer in project.mapLayers().items():
            if isinstance(layer, QgsRasterLayer):
                continue
            # If a shortname is set, we must use it instead of
            # the plain layer name
            layer_name = layer.shortName() or layer.name()
            if dict_key == 'name':
                layer_key = layer_name
            else:
                layer_key = layer_id
            if layer_keys:
                if dict_key == 'name' and layer_name not in layer_keys:
                    continue
                if dict_key == 'id' and layer_id not in layer_keys:
                    continue
            fields_by_layer[layer_key] = {}
            if dict_key == 'name':
                fields_by_layer[layer_name]['layer_id'] = layer_id
            else:
                fields_by_layer[layer_id]['layer_name'] = layer_name
            fields_by_layer[layer_key] = [
                field.name() for field in layer.fields()]
        response.setStatusCode(200)
        response.write(
            json.dumps(fields_by_layer, indent=4, sort_keys=True))

    def _get_styles_by_layer(self, request, response, project):
        if QgsServerProjectUtils.wmsUseLayerIds(project):
            dict_key = 'id'
        else:
            dict_key = 'name'
        try:
            layer_keys_str = request.parameters()['LAYERS']
        except KeyError:
            layer_keys_str = None
        if layer_keys_str:
            layer_keys = layer_keys_str.split(',')
        else:
            layer_keys = None
        styles_by_layer = {}
        for layer_id, layer in project.mapLayers().items():
            if isinstance(layer, QgsRasterLayer):
                continue
            # If a shortname is set, we must use it instead of
            # the plain layer name
            layer_name = layer.shortName() or layer.name()
            if dict_key == 'name':
                layer_key = layer_name
            else:
                layer_key = layer_id
            if layer_keys:
                if dict_key == 'name' and layer_name not in layer_keys:
                    continue
                if dict_key == 'id' and layer_id not in layer_keys:
                    continue
            styles_by_layer[layer_key] = {}
            if dict_key == 'name':
                styles_by_layer[layer_name]['layer_id'] = layer_id
            else:
                styles_by_layer[layer_id]['layer_name'] = layer_name
            styles_by_layer[layer_key] = layer.styleManager().styles()
        response.setStatusCode(200)
        response.write(
            json.dumps(styles_by_layer, indent=4, sort_keys=True))

    def _oq_engine_calc2map(self, request, response, project):
        # calculation_mode: 'scenario' or 'scenario_damage'
        calculation_mode = request.parameter('CALCULATION_MODE')
        method_name = '_oq_engine_calc2map_%s' % calculation_mode
        if not hasattr(self, method_name):
            response.setStatusCode(400)
            response.write(
                json.dumps({'status': 'fail',
                            'reason': 'no rules to produce maps for calculation_mode "%s".' %
                            calculation_mode}, indent=4, sort_keys=True))
            return

        eng_proto = 'http'
        eng_name = 'host.docker.internal'
        eng_port = '8800'
        engine_url = '%s://%s:%s' % (eng_proto, eng_name, eng_port)

        method = getattr(self, method_name)

        return method(request, response, project, engine_url)

    def _oq_engine_calc2map_scenario(self, request, response, project, engine_url):
        os.umask(0o0002)
        # Get the project instance
        project = QgsProject.instance()
        # Print the current project file name (might be
        # empty in case no projects have been loaded)
        # print(project.fileName())

        # create random prefix to avoid clash names

        gem_log('calc2map', Qgis.Critical)
        calc_id = request.parameter('CALC_ID')
        description = request.parameter('DESCRIPTION')
        imts = request.parameter('IMTS').split(',')

        session = Session()

        # engine_login(hostname, None, None, session)

        gem_log('calc2map: pre get', Qgis.Critical)
        oqparam_req = '%s/v1/calc/%d/extract/oqparam' % (
            engine_url, int(calc_id),)
        gem_log('calc2map: pre oq-param: [%s]' % oqparam_req, Qgis.Critical)
        resp = session.get(
            oqparam_req, timeout=100, verify=False, allow_redirects=False)

        gem_log('calc2map: post oq-param', Qgis.Critical)
        js = bytes(numpy.load(io.BytesIO(resp.content))['json'])
        calc = json.loads(js)

        calc_imtls = (calc['risk_imtls'] if 'risk_imtls' in calc
                      else calc['hazard_imtls'])
        if (set(imts) - set([x for x in calc_imtls])) != set():
            gem_log("calc2map: list of imt doesn't match calc imts",
                    Qgis.Critical)
            response.setStatusCode(500)
            response.write("calc2map: list of imt doesn't match calc imts")
            return

        # Clean current project
        project.clear()

        for i in range(0, 5):
            rnd_sfx = alphanum_rndstr(8)
            project_name = '%s_%s' % (description, rnd_sfx)
            lock_filename = '/io/uploads/projects/%s.lock' % project_name
            if acquire_lock(lock_filename):
                break
        else:
            gem_log('calc2map: lock of %s failed' % lock_filename,
                    Qgis.Critical)
            response.setStatusCode(500)
            response.write('calc2map: lock of %s failed' % lock_filename)
            return

            gem_log('calc2map: lock acquired %s' % lock_filename,
                    Qgis.Critical)

        # this 'try:' is to be able to unlock the locked file at the end of
        # project creation procedure
        try:
            # Load another project
            project.read('/io/data/_templates/Papers/PapersTmpl.qgs')
            gem_log('calc2map: project name: %s' % project.fileName(),
                    Qgis.Critical)

            # mkdir of base for all files of the project
            project_folder = '/io/uploads/projects/%s' % project_name
            layer_folder = '%s/layers' % project_folder
            os.mkdir(project_folder)
            os.mkdir(layer_folder)
            for imt in imts[::-1]:
                resp = session.get(
                    '%s/v1/calc/%d/extract/avg_gmf?imt=%s' % (
                        engine_url, int(calc_id), imt),
                    timeout=100, verify=False, allow_redirects=False)

                try:
                    if numpy.__version__ >= '1.24.0':
                        extracted_npz = numpy.load(
                            io.BytesIO(resp.content), allow_pickle=False,
                            max_header_size=100000)
                    else:
                        extracted_npz = numpy.load(
                            io.BytesIO(resp.content), allow_pickle=False)
                except Exception as exc:
                    print('FIXME: failure here %s' % exc)

                # New vector layer initialization
                layer = QgsVectorLayer("Point", imt, "memory")

                # Add fields to the layer
                layer.dataProvider().addAttributes([
                    QgsField(imt, QVariant.Double)
                ])
                layer.updateFields()

                extracted_tuples = numpy.column_stack((
                    extracted_npz['lons'], extracted_npz['lats'],
                    extracted_npz[imt]))

                #  import pdb ; _mute() ; pdb.set_trace()

                all_features = []
                with edit(layer):
                    # for idx in range(0, len(extracted_npz['lons'])):
                    # for idx in range(0, 100):
                    for idx, (lon, lat, imt_val) in enumerate(
                            extracted_tuples):
                        # if (idx % 1000) == 0:
                        #     print("Idx: %d, lon %f lat %f val %f" % (
                        #           idx, lon, lat, imt_val))

                        # if idx == 1000:
                        #     break

                        # Create some sample features
                        feature = QgsFeature()
                        feature.setGeometry(QgsGeometry.fromPointXY(
                            QgsPointXY(float(lon), float(lat))))
                        feature.setAttributes([float(imt_val)])
                        all_features.append(feature)

                    layer.dataProvider().addFeatures(all_features)

                    # Update the layer extension
                    layer.updateExtents()

                # Save layer as GeoPackage
                save_options = QgsVectorFileWriter.SaveVectorOptions()
                save_options.driverName = "GPKG"
                layer_name = imt
                save_options.layerName = layer_name

                gpkg_filepath = '%s/%s_%s_%s.gpkg' % (
                    layer_folder, description, imt, rnd_sfx)
                gem_log('calc2map: pre layer save [%s]' % gpkg_filepath,
                        Qgis.Critical)
                error = QgsVectorFileWriter.writeAsVectorFormat(
                    layer, gpkg_filepath,
                    "UTF-8", layer.crs(), "GPKG",
                    layerOptions=['OVERWRITE=YES'])

                gem_log('calc2map: post layer save', Qgis.Critical)

                if error[0] == QgsVectorFileWriter.NoError:
                    print("Layer save: success")
                else:
                    print("Layer save: error:", error)

                imt_layer = QgsVectorLayer(gpkg_filepath, layer_name, 'ogr')

                gem_log('IMT: %s' % imt, Qgis.Critical)

                _style_curves(imt_layer, imt)

                # add gpkg layer to current QGIS project
                project.addMapLayer(imt_layer)

                extent = layer.extent()
                ref_rect = QgsReferencedRectangle(extent, layer.crs())
                vs_project = project.viewSettings()
                vs_project.setDefaultViewExtent(ref_rect)

            gem_log('calc2map: pre project save', Qgis.Critical)

            # Create canvas
            canvas = QgsMapCanvas()
            canvas.setObjectName("theMapCanvas")
            # Set canvas size
            canvas.resize(QSize(800, 600))

            # FIXME: set proper values

            # Set coordinate reference system
            # crs = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas.setDestinationCrs(project.crs())

            # Set extent
            # extent = ref_rect  # QgsRectangle(-180, -90, 180, 90)

            layer_extent = extent
            source_crs = layer.crs()
            dest_crs = project.crs()
            transform = QgsCoordinateTransform(source_crs, dest_crs, project)
            transformed_extent = transform.transformBoundingBox(layer_extent)
            canvas.setExtent(transformed_extent)

            #
            #  save qgis project
            #
            project_filepath = '%s/%s.qgs' % (project_folder, project_name)
            project.write(project_filepath)
            project_filename = project.fileName()
            project.clear()
            gem_log('calc2map: post project save, filename [%s]' %
                    project_filename, Qgis.Critical)

            old_dir = os.getcwd()
            os.chdir(project_folder)

            gem_log('calc2map: chdir("%s")' % project_folder, Qgis.Critical)

            archive_pathname = '%s.zip' % project_folder
            zipdir(archive_pathname, '.')
            os.chdir(old_dir)

            if os.getenv('GEM_GV_KEEP_CALC_PROJECT', False) is False:
                rmdir_recursive(project_folder)

            gem_log('calc2map: post project zip', Qgis.Critical)

            response.setStatusCode(200)
            response.write(
                json.dumps({'owner': 'mop',
                            'uploaded_file': os.path.basename(
                                archive_pathname)},
                           indent=4, sort_keys=True))
        finally:
            release_lock(lock_filename)

    def _oq_engine_calc2map_scenario_damage(self, request, response, project, engine_url):
        # to speedup devel set it to a small value (100 is a good value)
        MAX_FEATURES =  os.getenv('GEM_GV_MAX_FEATURES', -1)

        # number of classified classes
        CLASSIFIED_CLASSES = 7

        # Get the project instance
        project = QgsProject.instance()

        gem_log('calc2map_scenario_damage:BEGIN', Qgis.Info)
        calc_id = request.parameter('CALC_ID')
        description = request.parameter('DESCRIPTION')
        adm_level = request.parameter('ADM_LEVEL')
        session = Session()

        oqparam_req = '%s/v1/calc/%d/results' % (
            engine_url, int(calc_id),)
        gem_log('calc2map: pre oq-param: [%s]' % oqparam_req, Qgis.Info)
        resp = session.get(
            oqparam_req, timeout=100, verify=False, allow_redirects=False)

        gem_log('calc2map: post oq-param', Qgis.Info)
        damages_stats_entries = [x for x in json.load(io.BytesIO(resp.content)) if
                              x['type'] == 'damages-stats']
        if len(damages_stats_entries) != 1:
            response.setStatusCode(500)
            response.write("calc2map: 'damages-stats' output not found")
            return

        # retrieve damages-stats output
        damages_stats = session.get("%s" % damages_stats_entries[0]['url'],
                                    timeout=600, verify=False, allow_redirects=False)

        # extract and populate exposure dictionary
        exposure_entries = [x for x in json.load(io.BytesIO(resp.content))
                            if x['type'] == 'exposure']
        if len(exposure_entries) != 1:
            response.setStatusCode(500)
            response.write("calc2map: 'exposure' output not found")
            return

        # retrieve damages-stats output
        exposure = session.get("%s" % exposure_entries[0]['url'],
                               timeout=600, verify=False, allow_redirects=False)

        with tempfile.TemporaryDirectory() as tempdir:
            arch_filename = os.path.join(tempdir, 'archive.zip')
            with open(arch_filename, "wb") as fp_out:
                fp_out.write(exposure.content)
            with ZipFile(arch_filename, "r") as zip_archive:
                zip_archive.extractall(tempdir)

            xml_files = [f for f in os.listdir(tempdir) if f.endswith('.xml')]

            if len(xml_files) != 1:
                gem_log('calc2map: more than one xml file in exposure [%s]' %
                        xml_files, Qgis.Critical)
                response.setStatusCode(500)
                response.write('calc2map: more than one xml file in exposure [%s]' %
                               xml_files)
                return

            tree = ET.parse(os.path.join(tempdir, xml_files[0]))
            ns = {'n': 'http://openquake.org/xmlns/nrml/0.5'}
            assets_tags = tree.findall("./n:exposureModel/n:assets", namespaces=ns)
            if len(assets_tags) != 1:
                gem_log('calc2map: multiple assets tags not supported [%s]' %
                        assets_tags, Qgis.Critical)
                response.setStatusCode(500)
                response.write('calc2map: multiple assets tags not supported [%s]' %
                               assets_tags)
                return

            csv_filenames = [x.strip() for x in assets_tags[0].text.split('\n')
                             if x.strip() != '']

            if len(csv_filenames) != 1:
                gem_log('calc2map: multiple assets elements not supported [%s]' %
                        csv_filenames, Qgis.Critical)
                response.setStatusCode(500)
                response.write(
                    'calc2map: multiple assets elements not supported [%s]' %
                    csv_filenames)
                return

            exposure_dict = {}
            with open(os.path.join(tempdir, csv_filenames[0]), 'r') as exposure_csv_fp:
                exposure_csv = csv.DictReader(exposure_csv_fp)
                for exposure_row in exposure_csv:
                    exposure_dict[exposure_row['id']] = {
                        'id': exposure_row['id'],
                        'lon': exposure_row['lon'],
                        'lat': exposure_row['lat'],
                        'value': sum([float(exposure_row['value-' + idx]) for idx in [
                            'structural', 'nonstructural', 'contents']])
                        }

            # HERE EXPOSURE DICT loaded correctly

            arch_filename = os.path.join(tempdir, 'archive.zip')
            with open(arch_filename, "wb") as fp_out:
                fp_out.write(exposure.content)

        # Clean current project
        project.clear()

        # create random prefix to avoid clash names
        for i in range(0, 5):
            rnd_sfx = alphanum_rndstr(8)
            project_name = '%s_%s' % (description, rnd_sfx)
            lock_filename = '/io/uploads/projects/%s.lock' % project_name
            if acquire_lock(lock_filename):
                break
        else:
            gem_log('calc2map: lock of %s failed' % lock_filename,
                    Qgis.Critical)
            response.setStatusCode(500)
            response.write('calc2map: lock of %s failed' % lock_filename)
            return

        gem_log('calc2map: lock acquired %s' % lock_filename,
                Qgis.Info)

        # this 'try...finally' is to be able to unlock the locked file at the end of
        # project creation procedure and remove temporary csv file
        try:
            fp_out = tempfile.NamedTemporaryFile(mode="w", delete=False)
            fp_in = io.StringIO(damages_stats.text)
            # skip info header
            next(fp_in)
            csv_in = csv.DictReader(fp_in)
            fieldnames = ['lon', 'lat', 'taxonomy', 'structural-complete',
                          'structural-losses', 'structural-fatalities']
            csv_out = csv.DictWriter(fp_out, fieldnames)
            csv_out.writeheader()
            for row_in in csv_in:
                csv_out.writerow({name: row_in[name] for name in fieldnames})
            fp_out.close()

            # create VectorLayer from filtered CSV file
            lines_to_skip_count = 0
            url = QUrl.fromLocalFile(fp_out.name)
            # url = QUrl("%s" % calc[0]['url'])
            url_query = QUrlQuery()
            url_query.addQueryItem('type', 'csv')
            url_query.addQueryItem('xField', 'lon')
            url_query.addQueryItem('yField', 'lat')
            url_query.addQueryItem('spatialIndex', 'yes')
            # url_query.addQueryItem('crs', 'epsg:4326')
            url_query.addQueryItem('subsetIndex', 'no')
            url_query.addQueryItem('watchFile', 'no')
            url_query.addQueryItem('delimiter', ',')
            url_query.addQueryItem('quote', '"')
            url_query.addQueryItem('skipLines', str(lines_to_skip_count))
            url_query.addQueryItem('trimFields', 'yes')
            url_query.addQueryItem('export_type', 'csv')
            url.setQuery(url_query)
            layer_uri = url.toString()
            gem_log('calc2map: layer uri: [%s]' % layer_uri, Qgis.Info)
            points_layer_csv = QgsVectorLayer(layer_uri, 'points_layer', 'delimitedtext')

            # create a in-memory layer copy for performance reason
            points_layer_csv.selectAll()
            points_layer = _create_layer_from_selected(points_layer_csv, with_attributes=True)
            points_layer_csv.removeSelection()

            if not points_layer.isValid():
                response.setStatusCode(500)
                response.write("calc2map: 'Unable to copy points_layer'")
                return

            # Load another project
            project.read('/io/data/_templates/Papers/PapersTmpl.qgs')
            gem_log('calc2map: project name: %s' % project.fileName(),
                    Qgis.Info)

            # mkdir of base for all files of the project
            project_folder = '/io/uploads/projects/%s' % project_name
            layer_folder = '%s/layers' % project_folder
            os.makedirs(project_folder)
            os.makedirs(layer_folder)

            processing.Processing.initialize()

            # INFO: here to change adm level if required
            # NAME_0 nation, NAME_1 region
            zonal_uri = ('/io/uploads/subdivision_areas/italy_adm%s.gpkg' % adm_level)
            zonal_layer = QgsVectorLayer(zonal_uri, 'italy_adm%s' % adm_level, 'ogr')

            # data structure for info about entire project
            quantity_keys = ['complete_damage', 'economic_losses', 'fatalities']

            proj_info = {}

            for quantity_key in quantity_keys:
                proj_info[quantity_key] = {'rank': []}
                for aggr_key in ['material', 'occupancy']:
                    proj_info[quantity_key][aggr_key] = {
                        'tot': {}
                    }

            # create destination layer making a copy of regions layer and adding a
            # couple of fields
            complete_damage_lay = QgsVectorLayer(
                'Polygon?crs=epsg:4326', 'Complete Damage', 'memory')
            complete_damage_dp = complete_damage_lay.dataProvider()
            complete_damage_lay.startEditing()

            economic_losses_lay = QgsVectorLayer(
                'Polygon?crs=epsg:4326', 'Economic Losses', 'memory')
            economic_losses_dp = economic_losses_lay.dataProvider()
            economic_losses_lay.startEditing()

            fatalities_lay = QgsVectorLayer(
                'Polygon?crs=epsg:4326', 'Fatalities', 'memory')
            fatalities_dp = fatalities_lay.dataProvider()
            fatalities_lay.startEditing()

            layer_descriptions = [
                (complete_damage_lay,
                 'complete_damage', 'Buildings beyond repair', 'Blues'),
                (economic_losses_lay,
                 'economic_losses', 'Economic Losses (USD)', 'Reds'),
                (fatalities_lay,
                 'fatalities', 'Fatalities', 'Greens')]

            # Adm 0 not included ID_0 = 'ITA', NAME_0 = 'Italy'
            for depth in range(1, int(adm_level) + 1):
                complete_damage_lay.addAttribute(QgsField(
                    'ID_%d' % depth, QVariant.Int))
                complete_damage_lay.addAttribute(QgsField(
                    'NAME_%d' % depth, QVariant.String))

                economic_losses_lay.addAttribute(QgsField(
                    'ID_%d' % depth, QVariant.Int))
                economic_losses_lay.addAttribute(QgsField(
                    'NAME_%d' % depth, QVariant.String))

                fatalities_lay.addAttribute(QgsField(
                    'ID_%d' % depth, QVariant.Int))
                fatalities_lay.addAttribute(QgsField(
                    'NAME_%d' % depth, QVariant.String))

            complete_damage_lay.addAttribute(QgsField('value', QVariant.Double))
            complete_damage_lay.addAttribute(QgsField('json_info', QVariant.String))
            complete_damage_lay.commitChanges()

            economic_losses_lay.addAttribute(QgsField('value', QVariant.Double))
            economic_losses_lay.addAttribute(QgsField('json_info', QVariant.String))
            economic_losses_lay.commitChanges()

            fatalities_lay.addAttribute(QgsField('value', QVariant.Double))
            fatalities_lay.addAttribute(QgsField('json_info', QVariant.String))
            fatalities_lay.commitChanges()

            # sequence to avoid usage of sites multiple times when on regions border
            grouped_sites = set()

            server_settings = QgsServerSettings()
            # loop on group layer and, for each feature select layer points features and
            # process them
            for zonal_feat in zonal_layer.getFeatures():
                points_layer.removeSelection()
                zonal_layer.selectByIds([zonal_feat.id()])

                temp_layer = _create_layer_from_selected(zonal_layer)

                # select sites intersect with single feature temp layer
                processing.run("native:selectbylocation", {
                    'INPUT': points_layer,
                    'PREDICATE': [0],  # 0 = intersects
                    'INTERSECT': temp_layer,
                    'METHOD': 0,
                    'SELECTED_FEATURES_ONLY': True,
                })

                # Remove temporary layer
                if temp_layer and temp_layer.id() in QgsProject.instance().mapLayers():
                    QgsProject.instance().removeMapLayer(temp_layer.id())

                # Or simply delete the layer object
                if temp_layer:
                    del temp_layer

                # if 'OUTPUT' in result:
                if points_layer.selectedFeatureIds():
                    if server_settings.logLevel() <= Qgis.Info:
                        gem_log('N FEATS: %d' % len(
                            points_layer.selectedFeatureIds()),
                                Qgis.Info)

                    # loop to populate new entry for metrics here
                    complete_damage_sum = 0.0
                    complete_damage_maggr = { 'material': {}, 'occupancy': {}}
                    economic_losses_sum = 0.0
                    economic_losses_maggr = { 'material': {}, 'occupancy': {}}
                    fatalities_sum =  0
                    fatalities_maggr = { 'material': {}, 'occupancy': {}}

                    for feat_idx, feat in enumerate(points_layer.selectedFeatures()):
                        if MAX_FEATURES != -1 and feat_idx == MAX_FEATURES:
                            break

                        if feat.id() in grouped_sites:
                            continue
                        else:
                            grouped_sites.add(feat.id())
                        aggregate_by = {'material': _get_material(feat['taxonomy']),
                                        'occupancy': _get_occupancy(feat['taxonomy'])}

                        complete_damage_sum += feat['structural-complete']
                        economic_losses_sum += feat['structural-losses']
                        fatalities_sum += feat['structural-fatalities']
                        for aggr_key, item_key in aggregate_by.items():
                            if item_key in complete_damage_maggr[aggr_key]:
                                complete_damage_maggr[aggr_key][item_key] += feat['structural-complete']
                            else:
                                complete_damage_maggr[aggr_key][item_key] = feat['structural-complete']


                            if item_key in economic_losses_maggr[aggr_key]:
                                economic_losses_maggr[aggr_key][item_key] += feat['structural-losses']
                            else:
                                economic_losses_maggr[aggr_key][item_key] = feat['structural-losses']


                            if item_key in fatalities_maggr[aggr_key]:
                                fatalities_maggr[aggr_key][item_key] += feat['structural-fatalities']
                            else:
                                fatalities_maggr[aggr_key][item_key] = feat['structural-fatalities']

                    for quantity_key in quantity_keys:
                        quantity_maggr = vars()[quantity_key + '_maggr']
                        is_first = True
                        super_tot = 0
                        for aggr_key in quantity_maggr:
                            for item_key, item_val in quantity_maggr[aggr_key].items():
                                if item_key not in proj_info[quantity_key][aggr_key]['tot']:
                                    proj_info[quantity_key][aggr_key]['tot'][item_key] = item_val
                                else:
                                    proj_info[quantity_key][aggr_key]['tot'][item_key] += item_val
                                if is_first:
                                    super_tot += item_val
                            is_first = False

                        rank_names = []
                        for depth in range(1, int(adm_level) + 1):
                            rank_names.append(zonal_feat['NAME_%d' % depth])

                        proj_info[quantity_key]['rank'].append({'id':zonal_feat.id(),
                                                                'names': rank_names, 'value': super_tot})

                        # sort ranked zones
                        new_rank = sorted(proj_info[quantity_key]['rank'], key=lambda d: d['value'], reverse=True)
                        proj_info[quantity_key]['rank'] = new_rank
                        # riduce rank to 10 elements
                        proj_info[quantity_key]['rank'] = proj_info[quantity_key]['rank'][:10]

                    # print(f"cdam: {complete_damage_sum}, ecloss: {economic_losses_sum},"
                    #       f" fatal: {fatalities_sum}")

                    for out_lay, out_dp, out_sum, out_json, out_name in [
                            (complete_damage_lay, complete_damage_dp, complete_damage_sum, complete_damage_maggr, 'complete_damage'),
                            (economic_losses_lay, economic_losses_dp, economic_losses_sum, economic_losses_maggr, 'economic_losses'),
                            (fatalities_lay, fatalities_dp, fatalities_sum, fatalities_maggr, 'fatalities')]:
                        if out_sum == 0.0:
                            continue
                        with edit(out_lay):
                            feat_out = QgsFeature(out_lay.fields())
                            feat_out.setGeometry(zonal_feat.geometry())
                            attrs = []
                            for depth in range(1, int(adm_level) + 1):
                                attrs += [zonal_feat['ID_%d' % depth],
                                          zonal_feat['NAME_%d' % depth]]
                            attrs += [json.dumps(out_sum), json.dumps(out_json)]
                            feat_out.setAttributes(attrs)
                            out_dp.addFeatures([feat_out])
                else:
                    print('N FEATS: ZERO')
                zonal_layer.removeSelection()

            #
            #  Analize data to create custom symbology < 1.0 + N groups with same numerosity
            #
            lays_values = {}
            lays_classes = {}
            for out_lay, out_filename, out_name, out_ramp in layer_descriptions:
                lays_values[out_filename] = []
                out_lay.selectAll()
                lay_values = lays_values[out_filename]
                attr_idx = -1
                # populate an ordered list of values
                for feat in out_lay.selectedFeatures():
                    if attr_idx < 0:
                        attr_idx = feat.fieldNameIndex('value')
                    bisect.insort(lay_values, float(feat.attributes()[attr_idx]))

                # identify the first value greater than one
                lt_one_idx = -1
                for idx, value in enumerate(lay_values):
                    if value > 1.0:
                        break
                    lt_one_idx = idx

                classified_classes_n = CLASSIFIED_CLASSES
                if lt_one_idx == -1:
                    # no lt_one case
                    classified_values = iter(lay_values)
                else:
                    # some elements has less than one value

                    classified_values = iter(lay_values[lt_one_idx:])
                    classified_classes_n -= 1

                cl_jenks = QgsClassificationJenks()
                cla = cl_jenks.classes(classified_values, classified_classes_n)
                lay_classes_float = (
                     cla if lt_one_idx == -1 else
                     [QgsClassificationRange('<= 1', float('-inf'), 1.0)] + cla)

                # create ceiled (integers as limits) ranges
                lay_classes = lays_classes[out_filename] = []
                for lay_class in lay_classes_float:
                    if lay_class.lowerBound() == float('-inf'):
                        lay_classes.append(lay_class)
                        last_upper = int(lay_class.upperBound())  # 1.0
                        continue

                    upper = math.ceil(lay_class.upperBound())
                    if upper == last_upper:
                        # if current class fit into the same ceiled upper-bounded
                        # range it will be skipped
                        continue

                    lower = last_upper
                    lay_classes.append(QgsClassificationRange(
                        "%s - %s" % (
                            f'{lower:,}', f'{upper:,}'),
                        lower, upper))
                    last_upper = upper

            default_qgs_style = QgsStyle().defaultStyle()
            default_color_ramp_names = default_qgs_style.colorRampNames()
            real_lays = []
            for out_lay, out_filename, out_name, out_ramp in layer_descriptions:
                lay_classes = lays_classes[out_filename]
                out_lay.startEditing()
                out_lay.selectAll()

                out_lay.updateExtents()
                gpkg_filepath = '%s/%s_%s.gpkg' % (
                    layer_folder, out_filename, rnd_sfx)

                gem_log('calc2map: pre layer save [%s]' % gpkg_filepath,
                        Qgis.Critical)
                out_lay.commitChanges()
                QgsVectorFileWriter.writeAsVectorFormat(
                    out_lay, gpkg_filepath,
                    "UTF-8", out_lay.crs(), "GPKG",
                    layerOptions=['OVERWRITE=YES'])
                print('calc2map: post layer save')

                out_real_layer = QgsVectorLayer(gpkg_filepath, out_name, 'ogr')
                real_lays.append(out_real_layer)
                symbol = QgsSymbol.defaultSymbol(out_real_layer.geometryType())
                symbol.setOpacity(1)
                ramp_type_idx = default_color_ramp_names.index(out_ramp)
                symbol.setColor(QColor(RAMP_EXTREME_COLORS[out_ramp]['top']))

                ramp = default_qgs_style.colorRamp(
                    default_color_ramp_names[ramp_type_idx])

                # ramp.invert() (to switch colors)

                # NOTE: get unique values not managed currently, check later
                # fni = out_real_layer.fields().indexOf('value')
                # unique_values = out_real_layer.dataProvider().uniqueValues(fni)
                # num_unique_values = len(unique_values - {NULL})

                # add a class for NULL values
                rule_renderer = QgsRuleBasedRenderer(symbol.clone())
                root_rule = rule_renderer.rootRule()
                gem_log('calc2map: len root_rule.children: %d' % len(
                    root_rule.children()), Qgis.Critical)

                gem_log('calc2map: pre loop lay_classes(%d)' % len(lay_classes),
                        Qgis.Critical)
                for cla_idx, cla in enumerate(lay_classes):
                    # filter = '"value" >= 0.000000 AND "value" <= 0.020361'
                    if cla.lowerBound() == float('-inf'):
                        upper = cla.upperBound()
                        filter = '"value" <= %s' % upper
                    else:
                        lower = cla.lowerBound()
                        upper = cla.upperBound()
                        filter = '"value" > %s AND "value" <= %s' % (
                            lower, upper)
                    cla_rule = rule_renderer.Rule(symbol.clone())

                    # ramp.color(0-1 included float)
                    color = ramp.color(
                        float(cla_idx) / float(len(lay_classes) - 1))
                    cla_rule.setSymbol(QgsFillSymbol.createSimple(
                    {'color':
                     '%d,%d,%d' % (color.red(), color.green(), color.blue())}))
                    cla_rule.setFilterExpression(filter)

                    # not_null_rule = root_rule.children()[0].clone()
                    # strip parentheses from stringified color HSL
                    # not_null_rule.setFilterExpression(
                    # '%s IS NOT NULL' % QgsExpression.quotedColumnRef(style_by))
                    cla_rule.setLabel(cla.label())
                    root_rule.appendChild(cla_rule)
                root_rule.removeChildAt(0)
                renderer = rule_renderer

                out_real_layer.setRenderer(renderer)
                out_real_layer.setId(out_filename)
                project.addMapLayer(out_real_layer)

                extent = out_real_layer.extent()
                gem_log('calc2map: extent of %s: %s' % (out_name, extent), Qgis.Critical)

                ref_rect = QgsReferencedRectangle(extent, out_real_layer.crs())
                vs_project = project.viewSettings()
                vs_project.setDefaultViewExtent(ref_rect)

            gem_log('calc2map: pre project save', Qgis.Critical)

            # Create canvas
            canvas = QgsMapCanvas()
            canvas.setObjectName("theMapCanvas")
            # Set canvas size
            canvas.resize(QSize(800, 600))

            # FIXME: set proper values

            # Set coordinate reference system
            # crs = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas.setDestinationCrs(project.crs())

            # Set extent
            # extent = ref_rect  # QgsRectangle(-180, -90, 180, 90)

            layer_extent = extent
            source_crs = out_lay.crs()
            dest_crs = project.crs()
            transform = QgsCoordinateTransform(source_crs, dest_crs, project)
            transformed_extent = transform.transformBoundingBox(layer_extent)
            canvas.setExtent(transformed_extent)

            canvas.refresh()

            #
            #  HOWTO: set project custom vars:
            #
            custom_vars = project.customVariables()
            custom_vars['project_info'] = json.dumps(proj_info)
            project.setCustomVariables(custom_vars)

            #
            #  save qgis project
            #
            project_filepath = '%s/%s.qgs' % (project_folder, project_name)
            project.write(project_filepath)

            project.clear()

            # gem_log('calc2map: post project save, filename [%s]' %
            #         project_filename, Qgis.Critical)
            old_dir = os.getcwd()
            os.chdir(project_folder)

            gem_log('calc2map: chdir("%s")' % project_folder, Qgis.Critical)

            archive_pathname = '%s.zip' % project_folder
            zipdir(archive_pathname, '.')
            os.chdir(old_dir)

            if os.getenv('GEM_GV_KEEP_CALC_PROJECT', False) is False:
                rmdir_recursive(project_folder)

            gem_log('calc2map: post project zip', Qgis.Critical)

            response.setStatusCode(200)
            response.write(
                json.dumps({'owner': 'mop',
                            'uploaded_file': os.path.basename(
                                archive_pathname)},
                           indent=4, sort_keys=True))

        finally:
            if os.path.exists(fp_out.name):
                os.unlink(fp_out.name)
            release_lock(lock_filename)
        return;



class EWM():

    def __init__(self, serverIface):
        self.serv = EWMS()
        serverIface.serviceRegistry().registerService(EWMS())
