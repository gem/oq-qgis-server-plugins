from qgis.server import QgsServerFilter
from qgis.core import QgsMessageLog


class OpacityDelegateFilter(QgsServerFilter):

    def __init__(self, serverIface):
        super().__init__(serverIface)

    def requestReady(self):
        print(dir(self))
        print(dir(self.serverInterface()))
        print(self.serverInterface().requestHandler().parameterMap())
        QgsMessageLog.logMessage("HelloFilter.requestReady")

    def sendResponse(self):
        QgsMessageLog.logMessage("HelloFilter.sendResponse")

    def responseComplete(self):
        QgsMessageLog.logMessage("HelloFilter.responseComplete")
        request = self.serverInterface().requestHandler()
        params = request.parameterMap()
        if params.get('SERVICE', '').upper() == 'HELLO':
            request.clear()
            request.setResponseHeader('Content-type', 'text/plain')
            # Note that the content is of type "bytes"
            request.appendBody(b'HelloServer!')


class OpacityDelegateServer:
    def __init__(self, serverIface):
        serverIface.registerFilter(OpacityDelegateFilter(serverIface), 100)
