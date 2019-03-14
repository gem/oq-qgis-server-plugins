import json
from qgis.server import QgsService
from qgis.core import QgsMessageLog


class OpenQuakeQGISWebServices(QgsService):

    def __init__(self):
        QgsService.__init__(self)

    def name(self):
        return "OQWS"

    def version(self):
        return "1.0.0"

    def allowMethod(method):
        return True

    def executeRequest(self, request, response, project):
        if request.parameter('REQUEST') == 'GetLayerCustomProperties':
            try:
                custom_props = {}
                for layer_id, layer in project.mapLayers().items():
                    layer_name = layer.name()
                    custom_props[layer_name] = {}
                    custom_props[layer_name]['layer_id'] = layer_id
                    for prop_key in layer.customPropertyKeys():
                        custom_props[layer_name][prop_key] = (
                            layer.customProperty(prop_key))
                response.setStatusCode(200)
                response.write(
                    json.dumps(custom_props, indent=4, sort_keys=True))
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        else:
            response.setStatusCode(400)
            response.write("Missing or invalid 'REQUEST' parameter")


class OpenQuakeQGIS():

    def __init__(self, serverIface):
        self.serv = OpenQuakeQGISWebServices()
        serverIface.serviceRegistry().registerService(
            OpenQuakeQGISWebServices())