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
import numpy
import json
from requests import Session

from qgis.server import QgsService, QgsServerProjectUtils

LOG_FI = '/io/data/spy.log'

from qgis.core import (
    QgsRasterLayer, QgsProject, QgsVectorLayer, QgsVectorFileWriter,
    QgsField, edit, QgsFeature, QgsPointXY, QgsGeometry,
    QgsReferencedRectangle, QgsSymbol, QgsGradientColorRamp,
    QgsApplication, QgsStyle, NULL, Qgis)
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import (QVariant, QSettings)

RAMP_EXTREME_COLORS = {
    'Reds':
        {'top': '#67000d',
         'bottom': '#fff5f0'},
    'Blues':
        {'top': '#08306b',
         'bottom': '#f7fbff'},
    'Greens':
        {'top': '#00441b',
         'bottom': '#f7fcf5'},
    'Spectral':
        {'top': '#d7191c',
         'bottom': '#2b83ba'}
}

if Qgis.QGIS_VERSION_INT < 31000:
    from qgis.core import QgsGraduatedSymbolRenderer
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


def get_style(layer, message_bar, restore_defaults=False):
    settings = QSettings()
    if restore_defaults:
        color_from_rgba = DEFAULT_SETTINGS['color_from_rgba']
    else:
        try:
            color_from_rgba = int(settings.value(
                'irmt/style_color_from',
                DEFAULT_SETTINGS['color_from_rgba']))
        except TypeError:
            msg = ('The type of the stored setting "style_color_from" was not'
                   ' valid, so the default has been restored.')
            if message_bar:
                log_msg(msg, level='C', message_bar=message_bar)
            else:
                print(msg)
            color_from_rgba = DEFAULT_SETTINGS['color_from_rgba']
    color_from = QColor().fromRgba(color_from_rgba)
    if restore_defaults:
        color_to_rgba = DEFAULT_SETTINGS['color_to_rgba']
    else:
        try:
            color_to_rgba = int(settings.value(
                'irmt/style_color_to',
                DEFAULT_SETTINGS['color_to_rgba']))
        except TypeError:
            msg = ('The type of the stored setting "style_color_to" was not'
                   ' valid, so the default has been restored.')
            if message_bar:
                log_msg(msg, level='C', message_bar=message_bar)
            else:
                print(msg)
            color_to_rgba = DEFAULT_SETTINGS['color_to_rgba']
    color_to = QColor().fromRgba(color_to_rgba)
    if Qgis.QGIS_VERSION_INT < 31000:
        style_mode = (DEFAULT_SETTINGS['style_mode']
                      if restore_defaults
                      else int(settings.value(
                          'irmt/style_mode', DEFAULT_SETTINGS['style_mode'])))
    else:
        style_mode = (DEFAULT_SETTINGS['style_mode']
                      if restore_defaults
                      else settings.value(
                          'irmt/style_mode', DEFAULT_SETTINGS['style_mode']))
    classes = (DEFAULT_SETTINGS['style_classes']
               if restore_defaults
               else int(settings.value(
                   'irmt/style_classes',
                   DEFAULT_SETTINGS['style_classes'])))
    # look for the setting associated to the layer if available
    force_restyling = None
    if layer is not None:
        # NOTE: We can't use %s/%s instead of %s_%s, because / is a special
        #       character
        value, found = QgsProject.instance().readBoolEntry(
            'irmt', '%s_%s' % (layer.id(), 'force_restyling'))
        if found:
            force_restyling = value
    if restore_defaults:
        force_restyling = DEFAULT_SETTINGS['force_restyling']
    # FIXME QGIS3: at project level, qgis pretends to find a value as false,
    #              even when it should be not found, so it prevents layers
    #              to be styled
    #
    # otherwise look for the setting at project level
    # if force_restyling is None:
    #     value, found = QgsProject.instance().readBoolEntry(
    #         'irmt', 'force_restyling')
    #     if found:
    #         force_restyling = value
    # if again the setting is not found, look for it at the general level
    if force_restyling is None:
        force_restyling = settings.value(
            'irmt/force_restyling',
            DEFAULT_SETTINGS['force_restyling'],
            type=bool)
    return {
        'color_from': color_from,
        'color_to': color_to,
        'style_mode': style_mode,
        'classes': classes,
        'force_restyling': force_restyling
    }

from qgis.core import Qgis, QgsMessageLog

from .prova import pippo


def gem_log(msg, log_level):
    QgsMessageLog.logMessage(msg + pippo, 'EWMS', log_level)


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
        gem_log('MOP WAS HERE', Qgis.Critical)

        if request.parameters()['REQUEST'] == 'GetLayerNames':
            try:
                self._get_layer_names(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayerCustomProperties':
            try:
                self._get_custom_properties_by_layer(
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
                self._oqengine_calc2map(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        else:
            response.setStatusCode(400)
            response.write("Missing or invalid 'REQUEST' parameter")

    def _get_layer_names(self, request, response, project):
        layer_names = [
            layer.name() for id, layer in project.mapLayers().items()]
        response.setStatusCode(200)
        response.write(
            json.dumps(layer_names, indent=4, sort_keys=True))

    def _get_custom_properties_by_layer(self, request, response, project):
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

    def _style_curves(self, layer, style_by):
        use_sgc_style = False
        opacity = 0.7

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
        if inverted:
            ramp.invert()

        ramp = default_qgs_style.colorRamp(
            default_color_ramp_names[ramp_type_idx])

        symbol.setColor(QColor(RAMP_EXTREME_COLORS['Reds']['top']))

        if inverted:
            ramp.invert()

        # get unique values
        fni = layer.fields().indexOf(style_by)
        unique_values = layer.dataProvider().uniqueValues(fni)
        num_unique_values = len(unique_values - {NULL})
        print('num_uniq: %d' % num_unique_values)
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

    def _oqengine_calc2map(self, request, response, project):
        # Get the project instance
        project = QgsProject.instance()
        # Print the current project file name (might be
        # empty in case no projects have been loaded)
        # print(project.fileName())

        print('here we are', file=open(LOG_FI, 'a'))
        calc_id = request.parameter('CALC_ID')
        imts = request.parameter('IMTS').split(',')

        hostname = 'http://127.0.0.1:8800'
        session = Session()

        # engine_login(hostname, None, None, session)

        # retrieve list of calculations
        resp = session.get(
            '%s/v1/calc/list' % hostname, timeout=10, verify=False,
            allow_redirects=False)

        resp = session.get(
            '%s/v1/calc/%d/extract/oqparam' % (hostname, int(calc_id),),
            timeout=100, verify=False, allow_redirects=False)

        js = bytes(numpy.load(io.BytesIO(resp.content))['json'])
        oqparam = json.loads(js)

        if (set(imts) - set([x for x in oqparam['hazard_imtls']])) != set():
            print('FIXME: failure here')

        # Clean current project
        project.clear()

        # Load another project
        project.read(
            '/home/nastasi/git/oq-geoviewer'
            '/project_samples/papers/PapersTmpl.qgs')
        print(project.fileName())
        for imt in imts:
            resp = session.get(
                'http://127.0.0.1:8800/v1/calc/%d/extract/avg_gmf?imt=%s' % (
                    int(calc_id), imt),
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
                # QgsField("id", QVariant.Int),
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
                    # import pdb ; _mute() ; pdb.set_trace()
                    if (idx % 1000) == 0:
                        print("Idx: %d" % idx)

                    # if idx == 1000:
                    #     break

                    # Create some sample features
                    feature = QgsFeature()
                    feature.setGeometry(QgsGeometry.fromPointXY(
                        QgsPointXY(lon, lat)))
                    feature.setAttributes([imt_val])
                    all_features.append(feature)

                print('Post loop')
                layer.dataProvider().addFeatures(all_features)

                # Update the layer extension
                layer.updateExtents()

            # Save layer as GeoPackage
            save_options = QgsVectorFileWriter.SaveVectorOptions()
            save_options.driverName = "GPKG"
            layer_name = imt
            save_options.layerName = layer_name
            gpkg_filepath = (
                '/home/nastasi/git/oq-geoviewer'
                '/project_samples/papers/out/Papers03_%s.gpkg' % imt)
            error = QgsVectorFileWriter.writeAsVectorFormat(
                layer, gpkg_filepath,
                "UTF-8", layer.crs(), "GPKG", layerOptions=['OVERWRITE=YES'])

            if error[0] == QgsVectorFileWriter.NoError:
                print("Layer save: success")
            else:
                print("Layer save: error:", error)

            imt_layer = QgsVectorLayer(gpkg_filepath, layer_name, 'ogr')

            _style_curves(imt_layer, imt)

            # add gpkg layer to current QGIS project
            QgsProject.instance().addMapLayer(imt_layer)

            extent = layer.extent()
            ref_rect = QgsReferencedRectangle(extent, layer.crs())
            vs_project = project.viewSettings()
            vs_project.setDefaultViewExtent(ref_rect)

        project.write(
            '/home/nastasi/git/oq-geoviewer'
            '/project_samples/papers/out/Papers03.qgz')

        response.setStatusCode(200)
        response.write(
            json.dumps({'owner': 'mop', 'test': project.fileName()},
                       indent=4, sort_keys=True))

class EWM():

    def __init__(self, serverIface):
        self.serv = EWMS()
        serverIface.serviceRegistry().registerService(EWMS())
